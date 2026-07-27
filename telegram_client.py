import asyncio
import json
import logging

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    UsernameNotModifiedError,
)
from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest
from telethon.tl.functions.photos import DeletePhotosRequest, UploadProfilePhotoRequest
from telethon.utils import get_input_photo

from providers import PROVIDERS, ProviderError, Session

log = logging.getLogger(__name__)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
EXIT_WORDS = ("exit", "quit", "выход", "стоп")
CHUNK = 4096

HELP = """Available commands

Chat
  .models          list models
  .model <name>    switch model
  .sessions        list saved sessions
  .resume <id>     continue a session
  .new             start a fresh session
  .effort <level>  low, medium, high, xhigh, max

Direct API mode
  .deepseek [model]  talk to DeepSeek instead of kiro-cli
  .use <provider>    same, generic form
  .providers         list configured providers
  exit               leave the mode (also quit / выход)

Profiles
  .profiles          list saved profiles
  .profile <name>    switch the bridge account's name/username/photo/bio
  .username <name>   set the username directly, .username - to clear it

Agents
  .agents          list agents
  .agent <name>    select agent

MCP
  .mcp             list servers
  .mcpstatus <n>   server status

Settings
  .settings        dump settings
  .set <k> <v>     change a setting

Other
  .whoami          current kiro account
  .status          bridge state
  .version         kiro-cli version
  .diag            run diagnostics
  .raw <args>      pass arguments straight to kiro-cli
  .kiro <message>  same as plain text below, explicit form

Anything not starting with a dot is forwarded to kiro-cli."""

MODE_HELP = """In-mode commands

  .model <name>   switch model
  .models         list models
  .think          toggle thinking mode
  .system <text>  set a system prompt, .system - to clear it
  .reset          forget the conversation
  .status         show mode state
  .exit           back to kiro-cli (also plain exit / quit / выход)"""


class Auth:
    """Holds pending login input so the web UI can supply it."""

    def __init__(self):
        self.needs_code = False
        self.needs_password = False
        self.code = None
        self.password = None
        self.error = None
        self.state = ""
        self._code = asyncio.Event()
        self._password = asyncio.Event()

    def give_code(self, value):
        self.code = value
        self.error = None
        self._code.set()

    def give_password(self, value):
        self.password = value
        self.error = None
        self._password.set()

    async def await_code(self):
        self.needs_code = True
        self.state = "waiting for login code"
        self._code.clear()
        await self._code.wait()
        self.needs_code = False
        return self.code

    async def await_password(self):
        self.needs_password = True
        self.state = "waiting for cloud password"
        self._password.clear()
        await self._password.wait()
        self.needs_password = False
        return self.password

    def reset(self):
        self.needs_code = self.needs_password = False
        self.code = self.password = self.error = None
        self.state = ""


auth = Auth()


class Bridge:
    def __init__(self, api_id, api_hash, phone, session_name, owner_id,
                 allowed_chats, kiro, keys=None, profiles=None):
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.session_name = session_name
        self.owner_id = owner_id
        self.allowed_chats = allowed_chats
        self.kiro = kiro
        self.keys = keys or {}
        self.profiles = profiles
        self.client = None
        self.active = False
        self.mode = None

    async def start(self):
        self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)
        await self.client.connect()

        if not await self.client.is_user_authorized():
            await self._login()

        me = await self.client.get_me()
        log.info("signed in as %s (@%s) id=%s", me.first_name, me.username, me.id)
        auth.state = "connected as %s" % me.first_name

        self._bind()
        self.active = True

    async def stop(self):
        self.active = False
        if self.client:
            await self.client.disconnect()
        log.info("disconnected")

    @property
    def connected(self):
        return self.active and self.client is not None and self.client.is_connected()

    async def wait(self):
        if self.client:
            await self.client.run_until_disconnected()

    # login -------------------------------------------------------------

    async def _login(self):
        auth.reset()

        for _ in range(5):
            try:
                sent = await self.client.send_code_request(self.phone)
                code = await auth.await_code()
                try:
                    await self.client.sign_in(
                        self.phone, code, phone_code_hash=sent.phone_code_hash
                    )
                    return
                except SessionPasswordNeededError:
                    await self._two_factor()
                    return
            except PhoneCodeInvalidError:
                auth.error = "That code was rejected. Try again."
            except PhoneCodeExpiredError:
                auth.error = "Code expired, sending a new one."
            except FloodWaitError as exc:
                auth.error = "Rate limited, waiting %ss" % exc.seconds
                await asyncio.sleep(exc.seconds)

        raise RuntimeError("login failed after several attempts")

    async def _two_factor(self):
        for _ in range(3):
            password = await auth.await_password()
            try:
                await self.client.sign_in(password=password)
                return
            except PasswordHashInvalidError:
                auth.error = "Wrong password."
        raise RuntimeError("two-factor password rejected")

    # dispatch ----------------------------------------------------------

    def allowed(self, sender_id, chat_id):
        """True only for the owner, and only in a whitelisted chat if one is set."""
        if not self.owner_id:
            return False
        if sender_id != self.owner_id:
            return False
        if self.allowed_chats and chat_id not in self.allowed_chats:
            return False
        return True

    def _bind(self):
        @self.client.on(events.NewMessage(incoming=True))
        async def _(event):
            if not self.allowed(event.sender_id, event.chat_id):
                log.debug("ignored sender=%s chat=%s", event.sender_id, event.chat_id)
                return

            text = event.raw_text
            if not text:
                return

            if self.mode:
                await self._in_mode(event, text)
            elif text.startswith("."):
                await self._command(event, text)
            else:
                await self._forward(event, text)

    # provider mode -----------------------------------------------------

    async def _in_mode(self, event, text):
        stripped = text.strip()

        if stripped.lower() in EXIT_WORDS or stripped.lower() == ".exit":
            label = self.mode.label
            self.mode = None
            await event.reply("Left %s mode, back on kiro-cli." % label)
            return

        if stripped.startswith("."):
            await self._mode_command(event, stripped)
            return

        await self._ask_provider(event, stripped)

    async def _mode_command(self, event, text):
        head, _, tail = text.partition(" ")
        name = head.lower()
        arg = tail.strip()

        if name == ".help":
            await self._send_mono(event, MODE_HELP)

        elif name == ".models":
            rows = []
            for model in self.mode.models:
                marker = ">" if model == self.mode.model else " "
                rows.append("%s %s" % (marker, model))
            await self._send_mono(event, "\n".join(rows))

        elif name == ".model":
            if not arg:
                await event.reply("Current model: %s" % self.mode.model)
                return
            try:
                self.mode.set_model(arg)
            except ProviderError as exc:
                await event.reply(str(exc))
            else:
                await event.reply("Model set to %s" % arg)

        elif name == ".think":
            if not self.mode.supports_thinking:
                await event.reply("%s has no thinking mode." % self.mode.label)
                return
            self.mode.thinking = not self.mode.thinking
            await event.reply("Thinking mode %s" % ("on" if self.mode.thinking else "off"))

        elif name == ".system":
            if not arg or arg == "-":
                self.mode.system = None
                await event.reply("System prompt cleared.")
            else:
                self.mode.system = arg
                await event.reply("System prompt set (%d chars)." % len(arg))

        elif name == ".reset":
            self.mode.reset()
            await event.reply("History cleared.")

        elif name == ".status":
            await self._send_mono(event, self._mode_status())

        else:
            await event.reply("Not available in %s mode. Send .help or exit." % self.mode.label)

    def _mode_status(self):
        thinking = "on" if self.mode.thinking else "off"
        system = "set (%d chars)" % len(self.mode.system) if self.mode.system else "none"
        return "\n".join([
            "provider: %s" % self.mode.label,
            "model:    %s" % self.mode.model,
            "thinking: %s" % (thinking if self.mode.supports_thinking else "n/a"),
            "system:   %s" % system,
            "turns:    %s" % (len(self.mode.history) // 2),
        ])

    async def _enter_mode(self, event, name, model=None):
        key = self.keys.get(PROVIDERS[name]["key"], "")
        try:
            self.mode = Session(
                name, key, model,
                max_length=getattr(self.kiro, "max_length", 4000),
            )
        except ProviderError as exc:
            await event.reply(str(exc))
            return

        await event.reply(
            "%s mode. Model %s. Send .exit to leave, .help for commands, .system to set a prompt."
            % (self.mode.label, self.mode.model)
        )

    async def _ask_provider(self, event, text):
        log.info("mode %s chat %s: %s", self.mode.name, event.chat_id, text[:60])
        placeholder = await event.reply("Working on it...")
        typing = asyncio.create_task(self._keep_typing(event.chat_id))

        try:
            reply = await self.mode.ask(text)
        except ProviderError as exc:
            reply = str(exc)
        except Exception as exc:
            log.exception("provider call failed")
            reply = "Failed: %s" % exc
        finally:
            typing.cancel()

        try:
            await placeholder.delete()
        except Exception:
            pass

        await self._send(event, reply)

    # kiro commands -----------------------------------------------------

    async def _command(self, event, text):
        head, _, tail = text.partition(" ")
        name = head.lower()
        arg = tail.strip()

        if name == ".help":
            await self._send_mono(event, HELP)

        elif name == ".providers":
            rows = []
            for key, spec in PROVIDERS.items():
                ready = "ready" if self.keys.get(spec["key"]) else "no key"
                rows.append("%-10s %-10s %s" % (key, ready, ", ".join(spec["models"])))
            await self._send_mono(event, "\n".join(rows), tail="Enter with .use <name>")

        elif name == ".use":
            provider, _, model = arg.partition(" ")
            if provider not in PROVIDERS:
                await event.reply("Known providers: %s" % ", ".join(PROVIDERS))
            else:
                await self._enter_mode(event, provider, model.strip() or None)

        elif name.lstrip(".") in PROVIDERS:
            await self._enter_mode(event, name.lstrip("."), arg or None)

        elif name == ".profiles":
            await self._list_profiles(event)

        elif name == ".profile":
            await self._switch_profile(event, arg)

        elif name == ".username":
            await self._set_username(event, arg)

        elif name == ".models":
            await self._list_models(event)

        elif name == ".model":
            if arg:
                self.kiro.model = arg
                await event.reply("Model set to %s" % arg)
            else:
                await event.reply("Current model: %s" % self.kiro.model)

        elif name == ".sessions":
            await self._list_sessions(event)

        elif name == ".resume":
            await self._resume(event, arg)

        elif name == ".new":
            self.kiro.session_id = None
            await event.reply("Started a new session.")

        elif name == ".effort":
            if arg in EFFORT_LEVELS:
                self.kiro.effort = arg
                await event.reply("Effort set to %s" % arg)
            else:
                await event.reply("Pick one of: %s" % ", ".join(EFFORT_LEVELS))

        elif name == ".agents":
            out = await self.kiro.run("agent list")
            await self._send_mono(event, out)

        elif name == ".agent":
            if arg:
                self.kiro.agent = arg
                await event.reply("Agent set to %s" % arg)
            else:
                await event.reply("Current agent: %s" % (self.kiro.agent or "default"))

        elif name == ".mcp":
            out = await self.kiro.run("mcp list")
            await self._send_mono(event, out)

        elif name == ".mcpstatus":
            if not arg:
                await event.reply("Usage: .mcpstatus <server>")
            else:
                out = await self.kiro.run(["mcp", "status", arg])
                await self._send_mono(event, out)

        elif name == ".settings":
            out = await self.kiro.run("settings list")
            await self._send_mono(event, out)

        elif name == ".set":
            key, _, value = arg.partition(" ")
            value = value.strip()
            if not value:
                await event.reply("Usage: .set <key> <value>")
            else:
                out = await self.kiro.run(["settings", key, value])
                await event.reply(out or "%s = %s" % (key, value))

        elif name == ".whoami":
            out = await self.kiro.run("whoami")
            await event.reply(out)

        elif name == ".version":
            out = await self.kiro.run("--version")
            await event.reply(out)

        elif name == ".status":
            await self._send_mono(event, self._status_text())

        elif name == ".diag":
            out = await self.kiro.run("diagnostic", timeout=90)
            await self._send_mono(event, out)

        elif name == ".raw":
            if not arg:
                await event.reply("Usage: .raw <kiro-cli arguments>")
            else:
                out = await self.kiro.run(arg, timeout=120)
                await self._send_mono(event, out)

        elif name == ".kiro":
            if not arg:
                await event.reply("Usage: .kiro <your message>")
            else:
                await self._forward(event, arg)

        else:
            await event.reply("Unknown command. Send .help for the list.")

    # command helpers ---------------------------------------------------

    # profiles ------------------------------------------------------

    async def _list_profiles(self, event):
        if not self.profiles:
            await event.reply("Profile storage is not set up.")
            return

        rows = self.profiles.all()
        if not rows:
            await event.reply("No saved profiles. Add them in the web ui.")
            return

        lines = []
        for p in rows:
            handle = "@" + p.username if p.username else ("(cleared)" if p.clear_username else "-")
            photo = "photo" if p.avatar else "no photo"
            lines.append("%-16s %-20s %-14s %s" % (p.name, p.first_name or "-", handle, photo))

        await self._send_mono(event, "\n".join(lines), tail="Switch with .profile <name>")

    async def _set_username_safe(self, username):
        """Apply a username change, ignoring the harmless case where it's already set."""
        try:
            await self.client(UpdateUsernameRequest(username=username))
        except UsernameNotModifiedError:
            pass

    async def _clear_avatars(self):
        """Remove every existing profile photo before a new one is uploaded."""
        photos = await self.client.get_profile_photos("me")
        ids = [get_input_photo(p) for p in photos]
        if ids:
            await self.client(DeletePhotosRequest(id=ids))

    async def _switch_profile(self, event, name):
        if not self.profiles:
            await event.reply("Profile storage is not set up.")
            return
        if not name:
            await event.reply("Usage: .profile <name>")
            return

        profile = self.profiles.get(name)
        if not profile:
            await event.reply("No profile called %s. See .profiles" % name)
            return

        if profile.avatar and not profile.avatar_path:
            await event.reply("Avatar file %s is missing, skipping the photo." % profile.avatar)

        try:
            # Telegram rejects an empty first_name, so only send what is filled in.
            fields = {}
            if profile.first_name:
                fields["first_name"] = profile.first_name
            if profile.about is not None:
                fields["about"] = profile.about
            if fields:
                await self.client(UpdateProfileRequest(**fields))

            if profile.username:
                await self._set_username_safe(profile.username)
            elif profile.clear_username:
                await self._set_username_safe("")

            avatar = profile.avatar_path
            if avatar:
                await self._clear_avatars()
                handle = await self.client.upload_file(str(avatar))
                await self.client(UploadProfilePhotoRequest(file=handle))

        except Exception as exc:
            log.exception("profile switch failed")
            await event.reply("Failed to switch profile: %s" % exc)
            return

        await event.reply("Switched to %s." % profile.name)

    async def _set_username(self, event, arg):
        value = arg.strip()
        if value.startswith("@"):
            value = value[1:]

        target = "" if value in ("", "-", "none", "off") else value

        try:
            await self.client(UpdateUsernameRequest(username=target))
        except UsernameNotModifiedError:
            await event.reply("Already set to that." if target else "Already cleared.")
            return
        except Exception as exc:
            await event.reply("Failed: %s" % exc)
            return

        await event.reply("Username cleared." if not target else "Username set to @%s" % target)

    # kiro helpers --------------------------------------------------

    async def _list_models(self, event):
        raw = await self.kiro.run("chat --list-models --format json")
        try:
            models = json.loads(raw)["models"]
        except (ValueError, KeyError, TypeError):
            await self._send_mono(event, raw)
            return

        rows = []
        for entry in models:
            if not isinstance(entry, dict):
                continue
            name = entry.get("model_name") or entry.get("model_id") or "?"
            marker = ">" if entry.get("model_id") == self.kiro.model else " "
            window = entry.get("context_window_tokens")
            window = "%4dK" % (window // 1000) if isinstance(window, int) else "   ?"
            rows.append("%s %-20s %s  x%s" % (
                marker, name, window, entry.get("rate_multiplier", "?")
            ))

        if not rows:
            await self._send_mono(event, raw)
            return

        await self._send_mono(event, "\n".join(rows), tail="Switch with .model <name>")

    async def _list_sessions(self, event):
        sessions = await self._sessions()
        if not sessions:
            await event.reply("No saved sessions.")
            return

        rows = []
        for entry in sessions[:15]:
            if not isinstance(entry, dict) or not entry.get("sessionId"):
                continue
            rows.append("%s  %-40s %s msgs" % (
                entry["sessionId"][:8],
                str(entry.get("title") or "untitled")[:40],
                entry.get("messageCount", 0),
            ))

        if not rows:
            await event.reply("No saved sessions.")
            return

        await self._send_mono(event, "\n".join(rows), tail="Resume with .resume <id>")

    async def _resume(self, event, arg):
        if not arg:
            await event.reply("Usage: .resume <id>")
            return

        target = arg
        for entry in await self._sessions():
            session_id = entry.get("sessionId") if isinstance(entry, dict) else None
            if session_id and session_id.startswith(arg):
                target = session_id
                break

        self.kiro.session_id = target
        await event.reply("Resuming %s" % target[:8])

    async def _sessions(self):
        raw = await self.kiro.run("chat --list-sessions --format json")
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        if isinstance(data, list) and data and isinstance(data[0], dict):
            sessions = data[0].get("sessions", [])
            return sessions if isinstance(sessions, list) else []
        if isinstance(data, dict):
            sessions = data.get("sessions", [])
            return sessions if isinstance(sessions, list) else []
        return []

    def _status_text(self):
        session = self.kiro.session_id[:8] if self.kiro.session_id else "new"
        return "\n".join([
            "backend: kiro-cli",
            "model:   %s" % self.kiro.model,
            "session: %s" % session,
            "agent:   %s" % (self.kiro.agent or "default"),
            "effort:  %s" % (self.kiro.effort or "default"),
        ])

    # prompt forwarding -------------------------------------------------

    async def _forward(self, event, text):
        log.info("chat %s: %s", event.chat_id, text[:60])
        placeholder = await event.reply("Working on it...")
        typing = asyncio.create_task(self._keep_typing(event.chat_id))

        try:
            reply = await self.kiro.ask(text)
        except Exception as exc:
            log.exception("prompt failed")
            reply = "Failed: %s" % exc
        finally:
            typing.cancel()

        try:
            await placeholder.delete()
        except Exception:
            pass

        await self._send(event, reply)

    async def _send(self, event, text):
        """Send free-form output as plain text, so stray markdown is not mangled."""
        for piece in _split(text, CHUNK):
            await self._reply(event, piece, parse_mode=None)

    async def _send_mono(self, event, text, tail=None):
        """Send output inside code blocks, splitting long output across messages."""
        body = (text or "").strip() or "(empty)"
        for piece in _split(_defuse_fences(body), CHUNK - 12):
            await self._reply(event, "```\n%s\n```" % piece)
        if tail:
            await self._reply(event, tail, parse_mode=None)

    async def _reply(self, event, text, **kwargs):
        try:
            await event.reply(text, **kwargs)
        except FloodWaitError as exc:
            log.warning("flood wait %ss", exc.seconds)
            await asyncio.sleep(exc.seconds)
            await event.reply(text, **kwargs)
        except Exception:
            log.exception("could not send a reply")
            return
        await asyncio.sleep(0.3)

    async def _keep_typing(self, chat_id):
        """Refresh the typing indicator until cancelled."""
        try:
            while True:
                try:
                    async with self.client.action(chat_id, "typing"):
                        await asyncio.sleep(4)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.debug("typing indicator failed", exc_info=True)
                    await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass


def _split(text, size):
    text = text if isinstance(text, str) else str(text)
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _defuse_fences(text):
    """Keep inner ``` from closing the code block we are about to open."""
    return text.replace("```", "``\u200b`")
