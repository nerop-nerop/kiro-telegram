"""Proves prompts reach kiro-cli as one argument, without shell interpretation.

Run: python test_command_build.py
"""

import asyncio
import sys

from kiro_bridge import Kiro, split_args


class RecordingKiro(Kiro):
    """Captures the argument list instead of spawning a process."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = []

    async def run(self, args, timeout=None):
        self.calls.append(args)
        return "ok"


async def main():
    failures = 0

    def check(label, condition):
        nonlocal failures
        if not condition:
            failures += 1
        print("%-4s %s" % ("ok" if condition else "FAIL", label))

    # a prompt is passed as a single argument, verbatim
    k = RecordingKiro()
    prompt = 'fix C:\\Users\\me\\proj & echo "oops" | del *\nsecond line'
    await k.ask(prompt)
    args = k.calls[-1]
    check("run got a list, not a shell string", isinstance(args, list))
    check("prompt survives untouched as the last argument", args[-1] == prompt)
    check("no quoting was injected into the prompt", '\\"' not in args[-1])
    check("base flags are present", args[:3] == ["chat", "--no-interactive", "--trust-all-tools"])

    # model, session, agent and effort become separate arguments
    k = RecordingKiro()
    k.model = "claude-opus-5"
    k.session_id = "abc123"
    k.agent = "default"
    k.effort = "high"
    await k.ask("hi")
    args = k.calls[-1]
    for flag, value in (("--model", "claude-opus-5"), ("--resume-id", "abc123"),
                        ("--agent", "default"), ("--effort", "high")):
        check("%s %s passed as its own argument" % (flag, value),
              flag in args and args[args.index(flag) + 1] == value)

    # auto model is left to kiro-cli
    k = RecordingKiro()
    await k.ask("hi")
    check("model auto adds no --model flag", "--model" not in k.calls[-1])

    # the string splitter keeps Windows paths intact and honours quotes
    check("backslashes are preserved",
          split_args(r"settings foo C:\Users\me") == ["settings", "foo", r"C:\Users\me"])
    check("quoted groups stay together",
          split_args('settings key "two words"') == ["settings", "key", "two words"])
    check("empty quoted argument is kept",
          split_args('settings key ""') == ["settings", "key", ""])
    check("blank input yields nothing", split_args("   ") == [])

    # long replies are truncated to the configured limit
    k = RecordingKiro(max_length=10)

    async def long_run(args, timeout=None):
        return "x" * 50

    k.run = long_run
    reply = await k.ask("hi")
    check("reply is truncated with a marker", reply.startswith("x" * 10) and "[truncated]" in reply)

    print()
    if failures:
        print("%d failures" % failures)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
