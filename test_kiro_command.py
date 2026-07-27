"""Proves .kiro forwards to kiro-cli exactly like plain text does.

Run: python test_kiro_command.py
"""

import asyncio
import sys

from telegram_client import Bridge


class FakeEvent:
    def __init__(self, chat_id=1):
        self.chat_id = chat_id
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


class FakeKiro:
    """Stands in for the real Kiro wrapper, records what it was asked."""

    def __init__(self):
        self.model = "auto"
        self.session_id = None
        self.agent = None
        self.effort = None
        self.received = []

    async def ask(self, prompt):
        self.received.append(prompt)
        return "kiro-reply: " + prompt

    async def run(self, args, timeout=None):
        if isinstance(args, (list, tuple)):
            args = " ".join(str(a) for a in args)
        return "ran: " + args


def make_bridge():
    return Bridge(
        api_id=1, api_hash="x", phone="+1", session_name="t",
        owner_id=1, allowed_chats=[], kiro=FakeKiro(),
    )


async def main():
    failures = 0

    def check(label, condition):
        nonlocal failures
        status = "ok" if condition else "FAIL"
        if not condition:
            failures += 1
        print("%-4s %s" % (status, label))

    # .kiro with a message forwards it, identically to plain text
    b = make_bridge()
    ev = FakeEvent()
    await b._command(ev, ".kiro what is the weather like")
    check(".kiro reaches kiro.ask", b.kiro.received == ["what is the weather like"])
    check("reply contains the kiro response", "kiro-reply:" in ev.replies[-1])

    # plain text (no dot) does the exact same thing
    b2 = make_bridge()
    ev2 = FakeEvent()
    await b2._forward(ev2, "what is the weather like")
    check("plain text also reaches kiro.ask", b2.kiro.received == ["what is the weather like"])
    check(".kiro and plain text produce the same downstream call",
          b.kiro.received == b2.kiro.received)

    # .kiro with no argument gives a usage hint instead of crashing
    b3 = make_bridge()
    ev3 = FakeEvent()
    await b3._command(ev3, ".kiro")
    check(".kiro with no text does not call kiro.ask", b3.kiro.received == [])
    check(".kiro with no text replies with usage", "Usage" in ev3.replies[-1])

    # a message that happens to start with a dot after the prefix is preserved verbatim
    b4 = make_bridge()
    ev4 = FakeEvent()
    await b4._command(ev4, ".kiro .help me with something")
    check("text after .kiro is passed through untouched",
          b4.kiro.received == [".help me with something"])

    print()
    if failures:
        print("%d failures" % failures)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
