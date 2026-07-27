"""Named profile presets: first name, username, bio, avatar file.
Switching one applies it to the bridge account via the Telegram API."""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

AVATAR_DIR = Path(__file__).parent / "avatars"
AVATAR_DIR.mkdir(exist_ok=True)


class Profile:
    def __init__(self, name, first_name="", username="", about="", avatar="",
                 clear_username=False):
        self.name = name
        self.first_name = first_name
        self.username = username
        self.about = about
        # Only ever a bare filename inside AVATAR_DIR. Stripping the directory
        # part here means a doctored config cannot point at an unrelated file.
        self.avatar = Path(avatar or "").name
        self.clear_username = clear_username  # True: strip username, leave username blank

    @property
    def avatar_path(self):
        if not self.avatar:
            return None

        path = (AVATAR_DIR / self.avatar).resolve()
        try:
            path.relative_to(AVATAR_DIR.resolve())
        except ValueError:
            log.warning("ignoring avatar outside the avatars folder: %r", self.avatar)
            return None

        return path if path.is_file() else None

    def to_dict(self):
        return {
            "name": self.name,
            "first_name": self.first_name,
            "username": self.username,
            "about": self.about,
            "avatar": self.avatar,
            "clear_username": self.clear_username,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            name=data["name"],
            first_name=data.get("first_name", ""),
            username=data.get("username", ""),
            about=data.get("about", ""),
            avatar=data.get("avatar", ""),
            clear_username=data.get("clear_username", False),
        )


class ProfileStore:
    """Backed by the same config object, key 'profiles'."""

    def __init__(self, config):
        self.config = config

    def all(self):
        raw = self.config.get("profiles") or []
        return [Profile.from_dict(p) for p in raw]

    def get(self, name):
        for p in self.all():
            if p.name.lower() == name.lower():
                return p
        return None

    def save(self, profile):
        profiles = self.all()
        profiles = [p for p in profiles if p.name.lower() != profile.name.lower()]
        profiles.append(profile)
        self.config.set("profiles", [p.to_dict() for p in profiles])

    def delete(self, name):
        profiles = [p for p in self.all() if p.name.lower() != name.lower()]
        self.config.set("profiles", [p.to_dict() for p in profiles])
