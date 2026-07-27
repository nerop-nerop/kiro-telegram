"""Checks that credentials can live in .env instead of config.json.

Run: python test_env.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config as config_module

FAILURES = []


def check(label, ok):
    if not ok:
        FAILURES.append(label)
    print("%-4s %s" % ("ok" if ok else "FAIL", label))


def write_env(text):
    path = Path(tempfile.mkdtemp()) / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def main():
    # --- parsing ---------------------------------------------------------
    for key in ("T_PLAIN", "T_QUOTED", "T_SQUOTED", "T_EXPORTED",
                "T_SPACED", "T_EMPTY", "T_WITH_EQUALS", "T_PRESET"):
        os.environ.pop(key, None)

    env = write_env(
        "# a comment\n"
        "\n"
        "T_PLAIN=hello\n"
        'T_QUOTED="quoted value"\n'
        "T_SQUOTED='single'\n"
        "export T_EXPORTED=exported\n"
        "  T_SPACED  =  padded  \n"
        "T_EMPTY=\n"
        "T_WITH_EQUALS=sk-abc=def==\n"
        "NOT_A_PAIR\n"
    )
    taken = config_module.load_env_file(env)

    check("plain value read", os.environ.get("T_PLAIN") == "hello")
    check("double quotes stripped", os.environ.get("T_QUOTED") == "quoted value")
    check("single quotes stripped", os.environ.get("T_SQUOTED") == "single")
    check("export prefix handled", os.environ.get("T_EXPORTED") == "exported")
    check("whitespace trimmed", os.environ.get("T_SPACED") == "padded")
    check("empty value kept as empty", os.environ.get("T_EMPTY") == "")
    check("value containing = survives", os.environ.get("T_WITH_EQUALS") == "sk-abc=def==")
    check("line without = ignored", "NOT_A_PAIR" not in os.environ)
    check("comment ignored", not any(k.startswith("#") for k in taken))

    # --- real environment wins over the file -----------------------------
    os.environ["T_PRESET"] = "from-shell"
    env2 = write_env("T_PRESET=from-file\n")
    config_module.load_env_file(env2)
    check("shell value is not overwritten by .env",
          os.environ.get("T_PRESET") == "from-shell")

    # --- missing file is fine --------------------------------------------
    check("missing .env returns nothing",
          config_module.load_env_file(Path(tempfile.mkdtemp()) / "nope.env") == [])

    # --- config reads through to the environment -------------------------
    os.environ["KIRO_BRIDGE_OWNER_ID"] = "424242"
    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"

    tmp_cfg = Path(tempfile.mkdtemp()) / "config.json"
    original = config_module.CONFIG_PATH
    config_module.CONFIG_PATH = tmp_cfg
    try:
        cfg = config_module.Config()
        cfg.set("owner_id", 111)          # a stale value in the file
        cfg.set("deepseek_api_key", "sk-from-file")

        check("int from env wins over the file", cfg.get("owner_id") == 424242)
        check("secret from env wins over the file",
              cfg.get("deepseek_api_key") == "sk-from-env")
        check("env_source names the variable",
              config_module.env_source("owner_id") == "KIRO_BRIDGE_OWNER_ID")
        check("env_source empty for a file-only key",
              config_module.env_source("web_port") == "")

        # the form must not write a value that the environment already dictates
        cfg.update({"owner_id": "999", "deepseek_api_key": "sk-typed-in-browser"})
        check("form cannot shadow an env-provided number", cfg.get("owner_id") == 424242)
        check("form cannot shadow an env-provided secret",
              cfg.get("deepseek_api_key") == "sk-from-env")

        view = cfg.public_view()
        check("public view flags env-provided keys",
              view["from_env"].get("owner_id") == "KIRO_BRIDGE_OWNER_ID")
        check("public view still hides secret values",
              view.get("deepseek_api_key") == "")
        check("public view marks the secret as set",
              view["secrets_set"].get("deepseek_api_key") is True)

        # a key with no env override is still editable
        cfg.update({"web_port": "9000"})
        check("keys without an env override remain editable", cfg.get("web_port") == 9000)
    finally:
        config_module.CONFIG_PATH = original
        for key in ("KIRO_BRIDGE_OWNER_ID", "DEEPSEEK_API_KEY"):
            os.environ.pop(key, None)

    print()
    if FAILURES:
        print("%d failures" % len(FAILURES))
        return 1
    print("all env checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
