"""Security regression tests.

Each check corresponds to a hole that was found and closed. Run with the
bridge stopped so it does not fight over config.json:

    python test_security.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config as config_module
from profiles import AVATAR_DIR, Profile


class Recorder:
    def __init__(self):
        self.failures = 0

    def check(self, label, ok):
        if not ok:
            self.failures += 1
        print("%-4s %s" % ("ok" if ok else "FAIL", label))


def detach_environment():
    """Hide the real .env so these checks exercise config.json only.

    The environment deliberately overrides the file, so leaving the developer's
    own credentials in place would make every write here look like it failed.
    """
    saved = {}
    for name in config_module.ENV_OVERRIDES.values():
        if name in os.environ:
            saved[name] = os.environ.pop(name)
    return saved


def restore_environment(saved):
    os.environ.update(saved)


def isolated_config():
    """A Config pointed at a throwaway file so the real one is untouched."""
    tmp = Path(tempfile.mkdtemp()) / "config.json"
    original = config_module.CONFIG_PATH
    config_module.CONFIG_PATH = tmp
    cfg = config_module.Config()
    config_module.CONFIG_PATH = original
    # keep writes going to the temp file
    cfg_save = cfg.save

    def save():
        real, config_module.CONFIG_PATH = config_module.CONFIG_PATH, tmp
        try:
            cfg_save()
        finally:
            config_module.CONFIG_PATH = real

    cfg.save = save
    return cfg, tmp


def main():
    r = Recorder()
    saved_env = detach_environment()
    try:
        return run_checks(r)
    finally:
        restore_environment(saved_env)


def run_checks(r):
    from fastapi.testclient import TestClient
    from web_app import create_app

    cfg, tmp_path = isolated_config()
    cfg.update({"api_hash": "SECRET_HASH", "kiro_api_key": "SECRET_KIRO",
                "deepseek_api_key": "SECRET_DS", "owner_id": "111"})

    class DummyController:
        def status(self):
            return {"running": False, "configured": True,
                    "owner_id": cfg.get("owner_id"), "allowed_chats": []}

        async def start(self):
            return {"ok": True, "message": "started"}

        async def stop(self):
            return {"ok": True, "message": "stopped"}

    app = create_app(cfg, DummyController())
    client = TestClient(app)

    # --- 1. secrets must never be sent to the browser -------------------
    body = client.get("/api/config").json()
    leaked = [k for k in ("api_hash", "kiro_api_key", "deepseek_api_key")
              if body.get(k)]
    r.check("GET /api/config does not return secret values", not leaked)
    r.check("GET /api/config reports which secrets are set",
            body.get("secrets_set", {}).get("api_hash") is True)
    raw = json.dumps(body)
    r.check("no secret string appears anywhere in the response",
            "SECRET_HASH" not in raw and "SECRET_KIRO" not in raw and "SECRET_DS" not in raw)

    # --- 2. a blank secret keeps the stored one, "-" clears it ----------
    client.post("/api/config", json={"api_hash": ""})
    r.check("blank secret keeps the stored value", cfg.get("api_hash") == "SECRET_HASH")
    client.post("/api/config", json={"api_hash": "-"})
    r.check("a dash clears the secret", cfg.get("api_hash") == "")
    cfg.update({"api_hash": "SECRET_HASH"})

    # --- 3. cross-site requests are refused ----------------------------
    hijack = client.post("/api/config", json={"owner_id": 999},
                         headers={"origin": "http://evil.example"})
    r.check("foreign Origin is rejected", hijack.status_code == 403)
    r.check("owner_id survived the foreign Origin attempt", cfg.get("owner_id") == 111)

    fetch_meta = client.post("/api/config", json={"owner_id": 999},
                             headers={"sec-fetch-site": "cross-site"})
    r.check("Sec-Fetch-Site: cross-site is rejected", fetch_meta.status_code == 403)
    r.check("owner_id survived the cross-site attempt", cfg.get("owner_id") == 111)

    # a form-style post is what a malicious page can send without preflight
    form_post = client.post("/api/config", data="owner_id=999",
                            headers={"content-type": "text/plain"})
    r.check("non-JSON content type is rejected", form_post.status_code == 415)
    r.check("owner_id survived the text/plain attempt", cfg.get("owner_id") == 111)

    # same-origin json still works
    good = client.post("/api/config", json={"owner_id": 222})
    r.check("normal same-origin save still works", good.json().get("ok") is True)
    r.check("owner_id was updated by the legitimate call", cfg.get("owner_id") == 222)

    # --- 4. profiles cannot be rewritten through the bulk config post ---
    client.post("/api/config", json={"profiles": [
        {"name": "evil", "avatar": "../../../../Windows/win.ini"}]})
    r.check("profiles are ignored by /api/config",
            not cfg.get("profiles"))

    # --- 5. avatar paths are confined to the avatars folder -------------
    escaped = Profile(name="x", avatar="../../../../Windows/win.ini")
    r.check("directory part is stripped from the avatar name",
            "/" not in escaped.avatar and "\\" not in escaped.avatar)
    r.check("an escaping avatar path resolves to nothing",
            escaped.avatar_path is None)

    missing = Profile(name="y", avatar="definitely_not_here.png")
    r.check("a missing avatar resolves to nothing", missing.avatar_path is None)

    # a real file inside the folder is still accepted
    probe = AVATAR_DIR / "_selftest.png"
    probe.write_bytes(b"x")
    try:
        good_profile = Profile(name="z", avatar="_selftest.png")
        r.check("a genuine avatar inside the folder is accepted",
                good_profile.avatar_path is not None)
    finally:
        probe.unlink(missing_ok=True)

    # --- 6. uploads are limited to image types --------------------------
    bad_upload = client.post("/api/profiles/avatar",
                             files={"file": ("payload.exe", b"MZ", "application/octet-stream")})
    r.check("non-image upload is refused", bad_upload.json().get("ok") is not True)

    # --- 7. token auth, once a password is set --------------------------
    cfg.update({"web_token": "hunter2"})
    r.check("request without the token is rejected",
            client.get("/api/config").status_code == 401)
    r.check("request with the token is accepted",
            client.get("/api/config", headers={"x-auth-token": "hunter2"}).status_code == 200)
    r.check("request with a wrong token is rejected",
            client.get("/api/config", headers={"x-auth-token": "wrong"}).status_code == 401)

    shutil.rmtree(tmp_path.parent, ignore_errors=True)

    print()
    if r.failures:
        print("%d failures" % r.failures)
        return 1
    print("all security checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
