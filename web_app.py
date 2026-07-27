import logging
import secrets
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from profiles import AVATAR_DIR, Profile, ProfileStore
from telegram_client import auth

TEMPLATES = Path(__file__).parent / "templates"
BUFFER = deque(maxlen=200)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
MAX_AVATAR_BYTES = 8_000_000
UPLOAD_PATH = "/api/profiles/avatar"


async def _field(request: Request, key: str):
    try:
        payload = await request.json()
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return payload.get(key) or ""


class BufferHandler(logging.Handler):
    def emit(self, record):
        BUFFER.append(self.format(record))


def capture_logs():
    handler = BufferHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    )
    logging.getLogger().addHandler(handler)


def create_app(config, controller) -> FastAPI:
    app = FastAPI(title="kiro bridge")
    templates = Jinja2Templates(directory=str(TEMPLATES))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Blocks other sites from driving this API through your browser.

        Without this, any page you visit could post to localhost and rewrite
        owner_id, which would hand command execution to someone else.
        """
        port = config.get("web_port")
        allowed_origins = {
            "http://localhost:%s" % port,
            "http://127.0.0.1:%s" % port,
            "http://[::1]:%s" % port,
        }
        host_header = request.headers.get("host")
        if host_header:
            allowed_origins.add("http://%s" % host_header)
            allowed_origins.add("https://%s" % host_header)

        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "cross-site request refused"}, status_code=403)

        origin = request.headers.get("origin")
        if origin and origin not in allowed_origins:
            return JSONResponse({"detail": "bad origin"}, status_code=403)

        if request.method not in ("GET", "HEAD", "OPTIONS"):
            ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
            wanted = "multipart/form-data" if request.url.path == UPLOAD_PATH else "application/json"
            if ctype != wanted:
                return JSONResponse(
                    {"detail": "expected %s" % wanted}, status_code=415
                )

        token = config.get("web_token")
        query_token = request.query_params.get("token")
        if token:
            supplied = (
                request.headers.get("x-auth-token")
                or request.cookies.get("bridge_token")
                or query_token
                or ""
            )
            if not secrets.compare_digest(str(supplied), str(token)):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)

        response = await call_next(request)

        if token and query_token and secrets.compare_digest(str(query_token), str(token)):
            response.set_cookie(
                "bridge_token", token, httponly=True, samesite="strict", max_age=2_592_000
            )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse("index.html", {"request": request})

    @app.get("/api/config")
    async def read_config():
        return config.public_view()

    @app.post("/api/config")
    async def write_config(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return {"ok": False, "message": "Invalid JSON"}
        if not isinstance(payload, dict):
            return {"ok": False, "message": "Expected an object"}

        skipped = config.update(payload)
        if skipped:
            return {"ok": False, "message": "Not a number: %s" % ", ".join(skipped)}
        return {"ok": True, "message": "Saved"}

    @app.get("/api/state")
    async def state():
        return controller.status()

    @app.post("/api/start")
    async def start():
        return await controller.start()

    @app.post("/api/stop")
    async def stop():
        return await controller.stop()

    @app.get("/api/login")
    async def login_state():
        return {
            "code": auth.needs_code,
            "password": auth.needs_password,
            "error": auth.error,
            "state": auth.state,
        }

    @app.post("/api/login/code")
    async def submit_code(request: Request):
        value = str(await _field(request, "code")).strip()
        if not value:
            return {"ok": False, "message": "Code is empty"}
        auth.give_code(value)
        return {"ok": True, "message": "Sent"}

    @app.post("/api/login/password")
    async def submit_password(request: Request):
        value = str(await _field(request, "password")).strip()
        if not value:
            return {"ok": False, "message": "Password is empty"}
        auth.give_password(value)
        return {"ok": True, "message": "Sent"}

    @app.get("/api/profiles")
    async def list_profiles():
        store = ProfileStore(config)
        return [p.to_dict() for p in store.all()]

    @app.post("/api/profiles")
    async def save_profile(request: Request):
        try:
            data = await request.json()
        except Exception:
            return {"ok": False, "message": "Invalid JSON"}
        if not isinstance(data, dict):
            return {"ok": False, "message": "Expected an object"}

        name = str(data.get("name") or "").strip()
        if not name:
            return {"ok": False, "message": "Name is required"}

        store = ProfileStore(config)
        profile = Profile(
            name=name,
            first_name=str(data.get("first_name") or ""),
            username=str(data.get("username") or "").lstrip("@"),
            about=str(data.get("about") or ""),
            avatar=Path(str(data.get("avatar") or "")).name,
            clear_username=bool(data.get("clear_username")),
        )
        store.save(profile)
        return {"ok": True, "message": "Saved"}

    @app.post("/api/profiles/{name}/delete")
    async def delete_profile(name: str):
        ProfileStore(config).delete(name)
        return {"ok": True, "message": "Removed"}

    @app.post("/api/profiles/avatar")
    async def upload_avatar(file: UploadFile):
        suffix = Path(file.filename or "avatar.jpg").suffix.lower() or ".jpg"
        if suffix not in IMAGE_SUFFIXES:
            return {"ok": False, "message": "Use one of: %s" % ", ".join(sorted(IMAGE_SUFFIXES))}

        stem = _slug(file.filename or "avatar")
        safe_name = "%s%s" % (stem, suffix)
        dest = AVATAR_DIR / safe_name

        counter = 1
        while dest.exists():
            safe_name = "%s_%d%s" % (stem, counter, suffix)
            dest = AVATAR_DIR / safe_name
            counter += 1

        written = 0
        try:
            with open(dest, "wb") as out:
                while True:
                    chunk = await file.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_AVATAR_BYTES:
                        raise ValueError("too large")
                    out.write(chunk)
        except ValueError:
            dest.unlink(missing_ok=True)
            return {"ok": False, "message": "File is larger than %d MB" % (MAX_AVATAR_BYTES // 1_000_000)}
        except OSError as exc:
            dest.unlink(missing_ok=True)
            return {"ok": False, "message": "Could not save: %s" % exc}

        return {"ok": True, "filename": safe_name}

    @app.get("/api/logs")
    async def logs():
        return {"lines": list(BUFFER)}

    return app


def _slug(text: str) -> str:
    stem = Path(text).stem
    return "".join(c if c.isalnum() else "_" for c in stem)[:40] or "avatar"
