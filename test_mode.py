"""Proves prompts route to DeepSeek in-mode and .exit leaves the mode.

Run: python test_mode.py
"""

import asyncio
import sys

from telegram_client import Bridge
from providers import Session


class FakeEvent:
    """Minimal stand-in for a Telethon event, just captures replies."""

    def __init__(self, chat_id=1):
        self.chat_id = chat_id
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


class FakeSession(Session):
    """A Session that never calls the network, echoes what it received."""

    def __init__(self):
        self.name = "deepseek"
        self.spec = {"label": "DeepSeek", "models": ("deepseek-v4-flash", "deepseek-v4-pro"), "thinking": True}
        self.api_key = "test"
        self.model = "deepseek-v4-flash"
        self.thinking = False
        self.effort = "medium"
        self.history = []
        self.system = None
        self.received = []

    async def ask(self, prompt):
        self.received.append(prompt)
        self.history.append({"role": "user", "content": prompt})
        self.history.append({"role": "assistant", "content": "echo: " + prompt})
        return "echo: " + prompt


def make_bridge():
    b = Bridge(
        api_id=1, api_hash="x", phone="+1", session_name="t",
        owner_id=1, allowed_chats=[], kiro=None,
    )
    b.mode = FakeSession()
    return b


async def main():
    failures = 0

    def check(label, condition):
        nonlocal failures
        status = "ok" if condition else "FAIL"
        if not condition:
            failures += 1
        print("%-4s %s" % (status, label))

    # 1. a free-form prompt while in mode goes to the provider, not kiro-cli
    b = make_bridge()
    ev = FakeEvent()
    await b._in_mode(ev, "how does a diode work")
    check(
        "free text is forwarded to the provider",
        b.mode is not None and b.mode.received == ["how does a diode work"],
    )
    check("reply came back through the event", ev.replies and "echo:" in ev.replies[-1])

    # 2. custom system prompt can be set from chat and is picked up
    b = make_bridge()
    ev = FakeEvent()
    await b._in_mode(ev, ".system You are a pirate. Speak in pirate slang.")
    check("system prompt stored", b.mode.system == "You are a pirate. Speak in pirate slang.")
    check("confirmation reply sent", "System prompt set" in ev.replies[-1])

    # 3. .system - clears it
    ev = FakeEvent()
    await b._in_mode(ev, ".system -")
    check("system prompt cleared", b.mode.system is None)

    # 4. .exit leaves the mode
    b = make_bridge()
    ev = FakeEvent()
    await b._in_mode(ev, ".exit")
    check(".exit clears the mode", b.mode is None)
    check(".exit confirms leaving", "Left DeepSeek mode" in ev.replies[-1])

    # 5. plain "exit" still works too, for backward compatibility
    b = make_bridge()
    ev = FakeEvent()
    await b._in_mode(ev, "exit")
    check("plain exit also clears the mode", b.mode is None)

    # 6. after leaving the mode, an unrelated dot command must not be
    #    swallowed by mode handling (mode is None, so caller must route
    #    to _command instead -- verified structurally, not re-run here)
    check(
        "mode is falsy once left, so dispatcher in _bind would route to _command",
        b.mode is None,
    )

    print()
    if failures:
        print("%d failures" % failures)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
