"""Checks that the bridge only ever serves the owner.

Run: python test_access.py
"""

import sys

from telegram_client import Bridge

OWNER = 100000001
STRANGER = 100000002
BRIDGE_ITSELF = 100000003
GROUP = -1000000000001
OTHER_GROUP = -1000000000002


def make(allowed_chats=None, owner=OWNER):
    return Bridge(
        api_id=1,
        api_hash="x",
        phone="+70000000000",
        session_name="test",
        owner_id=owner,
        allowed_chats=allowed_chats or [],
        kiro=None,
    )


CASES = [
    # description, bridge, sender, chat, expected
    ("owner in private chat", make(), OWNER, OWNER, True),
    ("owner in a group", make(), OWNER, GROUP, True),
    ("stranger in private chat", make(), STRANGER, STRANGER, False),
    ("stranger in a group", make(), STRANGER, GROUP, False),
    ("bridge's own message", make(), BRIDGE_ITSELF, GROUP, False),
    ("anonymous admin / channel post", make(), None, GROUP, False),
    ("sender id zero", make(), 0, GROUP, False),
    ("owner id lookalike as string", make(), str(OWNER), GROUP, False),
    ("owner, chat whitelisted", make([GROUP]), OWNER, GROUP, True),
    ("owner, chat not whitelisted", make([GROUP]), OWNER, OTHER_GROUP, False),
    ("stranger, chat whitelisted", make([GROUP]), STRANGER, GROUP, False),
    ("owner_id not configured", make(owner=0), OWNER, GROUP, False),
    ("owner_id not configured, sender 0", make(owner=0), 0, GROUP, False),
]


def main():
    failures = 0

    for label, bridge, sender, chat, expected in CASES:
        actual = bridge.allowed(sender, chat)
        ok = actual is expected
        if not ok:
            failures += 1
        print("%-4s %-34s sender=%-12s -> %s" % (
            "ok" if ok else "FAIL", label, sender, actual
        ))

    print()
    if failures:
        print("%d of %d cases failed" % (failures, len(CASES)))
        return 1

    print("all %d cases passed" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
