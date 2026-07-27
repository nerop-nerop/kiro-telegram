import json
import os
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
ENV_PATH = HERE / ".env"


def load_env_file(path=None):
    """Read .env into the process environment.

    Real environment variables win, so exporting something in the shell still
    overrides the file. Returns the keys that were taken from the file.
    """
    path = Path(path) if path else ENV_PATH
    if not path.is_file():
        return []

    taken = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()

        key, sep, value = line.partition("=")
        if not sep:
            continue

        key = key.strip()
        if not key or key in os.environ:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        os.environ[key] = value
        taken.append(key)

    return taken


ENV_KEYS_LOADED = load_env_file()

DEFAULTS = {
    "api_id": 0,
    "api_hash": "",
    "phone": "",
    "owner_id": 0,
    "allowed_chats": [],
    "kiro_api_key": "",
    "deepseek_api_key": "",
    "profiles": [],
    "kiro_working_dir": ".",
    "web_host": "127.0.0.1",
    "web_port": 8080,
    "web_token": "",
    "session_name": "session",
    "max_response_length": 4000,
    "response_timeout": 180,
}

REQUIRED = ("api_id", "api_hash", "phone", "owner_id")

# Never sent back to the browser. Submitting an empty value keeps the stored
# one, submitting "-" clears it.
SECRET_KEYS = ("api_hash", "kiro_api_key", "deepseek_api_key", "web_token")

# Written only through their own endpoints, never through a bulk config post.
PROTECTED_KEYS = ("profiles",)

# Anything set in the environment wins over config.json, so credentials can be
# kept out of the file entirely.
ENV_OVERRIDES = {
    "api_id": "KIRO_BRIDGE_API_ID",
    "api_hash": "KIRO_BRIDGE_API_HASH",
    "phone": "KIRO_BRIDGE_PHONE",
    "owner_id": "KIRO_BRIDGE_OWNER_ID",
    "kiro_api_key": "KIRO_API_KEY",
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "web_token": "KIRO_BRIDGE_WEB_TOKEN",
}


def env_source(key: str) -> str:
    """Name of the environment variable supplying this key, if any."""
    name = ENV_OVERRIDES.get(key)
    if name and os.environ.get(name, "").strip():
        return name
    return ""


class Config:
    def __init__(self):
        self._data = {}
        self.load()

    def load(self):
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    self._data = json.load(f)
            except (ValueError, OSError):
                # A corrupt file must not stop the bridge from starting.
                self._data = dict(DEFAULTS)
            if not isinstance(self._data, dict):
                self._data = dict(DEFAULTS)
        else:
            self._data = dict(DEFAULTS)
            self.save()

    def save(self):
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        tmp.replace(CONFIG_PATH)

    def get(self, key: str, fallback: Any = None) -> Any:
        env = os.environ.get(ENV_OVERRIDES.get(key, ""), "").strip() if key in ENV_OVERRIDES else ""
        if env:
            if isinstance(DEFAULTS.get(key), int):
                try:
                    return int(env)
                except ValueError:
                    pass
            else:
                return env
        return self._data.get(key, DEFAULTS.get(key, fallback))

    def set(self, key: str, value: Any):
        self._data[key] = value
        self.save()

    def update(self, values: dict):
        skipped = []
        for key, value in values.items():
            if key not in DEFAULTS or key in PROTECTED_KEYS:
                continue

            # Writing this would be pointless: the environment wins on read,
            # so the file value would never be used and the two would drift.
            if env_source(key):
                continue

            if key in SECRET_KEYS:
                text = value.strip() if isinstance(value, str) else value
                if text == "" or text is None:
                    continue          # left blank in the form, keep what we have
                if text == "-":
                    self._data[key] = ""
                    continue
                self._data[key] = text
                continue

            expected = type(DEFAULTS[key])
            if expected is int and isinstance(value, str):
                text = value.strip()
                try:
                    value = int(text) if text else 0
                except ValueError:
                    skipped.append(key)
                    continue
            elif key == "allowed_chats":
                try:
                    if isinstance(value, str):
                        value = [int(x) for x in value.replace(" ", "").split(",") if x]
                    elif isinstance(value, list):
                        value = [int(x) for x in value]
                    else:
                        skipped.append(key)
                        continue
                except (ValueError, TypeError):
                    skipped.append(key)
                    continue
            self._data[key] = value
        self.save()
        return skipped

    def all(self) -> dict:
        return dict(self._data)

    def public_view(self) -> dict:
        """Config for the browser: secret values replaced by a set/unset flag."""
        data = {}
        for key in DEFAULTS:
            if key in PROTECTED_KEYS:
                continue
            data[key] = "" if key in SECRET_KEYS else self.get(key)
        data["secrets_set"] = {key: bool(self.get(key)) for key in SECRET_KEYS}
        data["from_env"] = {
            key: env_source(key) for key in ENV_OVERRIDES if env_source(key)
        }
        return data

    def is_complete(self) -> bool:
        return all(self.get(k) for k in REQUIRED)


config = Config()
