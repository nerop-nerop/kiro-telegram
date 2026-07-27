import asyncio
import logging
import os
import re
import shutil

log = logging.getLogger(__name__)

# CSI sequences (colours, cursor moves) and OSC sequences (window titles).
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

BINARY = "kiro-cli"


def split_args(text):
    """Split a command string into tokens.

    Double quotes group words, everything else is taken literally, so Windows
    paths like C:\\Users\\me keep their backslashes. Used only for the fixed
    internal commands and for .raw.
    """
    tokens, current, quoted, started = [], [], False, False

    for ch in text:
        if ch == '"':
            quoted = not quoted
            started = True
        elif ch.isspace() and not quoted:
            if started:
                tokens.append("".join(current))
                current, started = [], False
        else:
            current.append(ch)
            started = True

    if started:
        tokens.append("".join(current))
    return tokens


class Kiro:
    """Wraps kiro-cli invocations."""

    def __init__(self, cwd=".", timeout=180, max_length=4000, api_key=""):
        self.cwd = cwd
        self.timeout = timeout
        self.max_length = max_length
        self.api_key = api_key
        self.model = "auto"
        self.session_id = None
        self.agent = None
        self.effort = None

    def _env(self):
        env = os.environ.copy()
        if self.api_key:
            env["KIRO_API_KEY"] = self.api_key
        return env

    async def run(self, args, timeout=None):
        """Run kiro-cli.

        `args` is either a list of arguments (preferred, nothing is re-parsed)
        or a string that gets split on whitespace outside double quotes. No
        shell is involved, so message text can contain any characters.
        """
        tokens = [str(a) for a in args] if isinstance(args, (list, tuple)) else split_args(args)
        if not tokens:
            return "No command given"

        exe = shutil.which(BINARY)
        if not exe:
            return f"{BINARY} not found in PATH"

        limit = timeout or self.timeout
        proc = None

        try:
            proc = await asyncio.create_subprocess_exec(
                exe,
                *tokens,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=self._env(),
            )
            out, err = await asyncio.wait_for(proc.communicate(), limit)
        except asyncio.TimeoutError:
            await _terminate(proc)
            return f"Timed out after {limit}s"
        except asyncio.CancelledError:
            await _terminate(proc)
            raise
        except FileNotFoundError:
            return f"{BINARY} not found in PATH"
        except Exception as exc:
            log.exception("kiro-cli failed")
            return str(exc)

        text = _decode(out) or _decode(err)
        return ANSI.sub("", text).strip()

    async def ask(self, prompt):
        """Send a prompt using the currently selected model, session and agent."""
        args = ["chat", "--no-interactive", "--trust-all-tools"]

        if self.model and self.model != "auto":
            args += ["--model", self.model]
        if self.session_id:
            args += ["--resume-id", self.session_id]
        if self.agent:
            args += ["--agent", self.agent]
        if self.effort:
            args += ["--effort", self.effort]

        args.append(prompt)

        log.info("prompt -> %s (%s)", self.model, prompt[:60].replace("\n", " "))
        reply = await self.run(args)

        if len(reply) > self.max_length:
            reply = reply[: self.max_length] + "\n\n[truncated]"
        return reply or "Empty response"


async def _terminate(proc):
    """Make sure a finished-with process is really gone."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    except Exception:
        log.debug("could not kill kiro-cli", exc_info=True)
        return
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), 5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
