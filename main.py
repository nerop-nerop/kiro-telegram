import asyncio
import logging
from logging.handlers import RotatingFileHandler

import uvicorn

from config import config
from kiro_bridge import Kiro
from profiles import ProfileStore
from telegram_client import Bridge
from web_app import capture_logs, create_app

log = logging.getLogger(__name__)


def configure_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    rotating = RotatingFileHandler(
        "bridge.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"
    )
    rotating.setFormatter(fmt)
    root.addHandler(rotating)

    logging.getLogger("telethon").setLevel(logging.WARNING)
    capture_logs()


class Controller:
    def __init__(self):
        self.bridge = None
        self.task = None

    def status(self):
        return {
            "running": bool(self.bridge and self.bridge.connected),
            "configured": config.is_complete(),
            "owner_id": config.get("owner_id"),
            "allowed_chats": config.get("allowed_chats"),
        }

    async def start(self):
        if self.task and not self.task.done():
            return {"ok": False, "message": "Already running"}
        if not config.is_complete():
            return {"ok": False, "message": "Fill in the credentials first"}

        self.task = asyncio.create_task(self._run())
        return {"ok": True, "message": "Connecting"}

    async def stop(self):
        task, self.task = self.task, None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("bridge task ended badly")
        if self.bridge:
            try:
                await self.bridge.stop()
            except Exception:
                log.exception("could not stop the bridge cleanly")
            self.bridge = None
        return {"ok": True, "message": "Stopped"}

    async def _run(self):
        kiro = Kiro(
            cwd=config.get("kiro_working_dir"),
            timeout=config.get("response_timeout"),
            max_length=config.get("max_response_length"),
            api_key=config.get("kiro_api_key"),
        )

        self.bridge = Bridge(
            api_id=config.get("api_id"),
            api_hash=config.get("api_hash"),
            phone=config.get("phone"),
            session_name=config.get("session_name"),
            owner_id=config.get("owner_id"),
            allowed_chats=config.get("allowed_chats"),
            kiro=kiro,
            keys={"deepseek_api_key": config.get("deepseek_api_key")},
            profiles=ProfileStore(config),
        )

        try:
            await self.bridge.start()
            await self.bridge.wait()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("bridge stopped: %s", exc)


async def main():
    configure_logging()

    controller = Controller()
    app = create_app(config, controller)
    host = config.get("web_host") or "127.0.0.1"
    port = config.get("web_port")
    token = config.get("web_token")

    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not token:
        log.error(
            "refusing to listen on %s without a password: anyone who can reach this "
            "port could read your api keys and take over the bridge. Set web_token, "
            "or put web_host back to 127.0.0.1.", host
        )
        return

    log.info("web ui on http://%s:%s", "localhost" if loopback else host, port)
    if not loopback:
        log.warning("web ui is reachable from the network; open it once with "
                    "?token=... to store the cookie")

    if config.is_complete():
        await controller.start()
    else:
        log.info("credentials missing, open the web ui to configure")

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning")
    )

    try:
        await server.serve()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await controller.stop()


if __name__ == "__main__":
    asyncio.run(main())
