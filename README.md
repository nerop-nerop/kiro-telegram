# kiro bridge

Talk to [`kiro-cli`](https://kiro.dev/docs/cli/) from Telegram.

A spare Telegram account acts as the bridge. It listens for messages from your
own account, runs them through `kiro-cli`, and sends the answer back. There is a
small web page for configuration and logs.

```
you ──▶ bridge account ──▶ kiro-cli ──▶ bridge account ──▶ you
```

Everything runs on your machine; nothing is proxied through a third party.

## Requirements

- Python 3.10 or newer
- `kiro-cli`, either already logged in (`kiro-cli login`) or with an API key
- A second Telegram account for the bridge

## Install

```
git clone https://github.com/<you>/kiro-bridge.git
cd kiro-bridge
python -m venv venv
venv\Scripts\activate          # Linux and macOS: source venv/bin/activate
pip install -r requirements.txt
```

## Configure

Copy the template and fill in what you need:

```
copy .env.example .env         # Linux and macOS: cp .env.example .env
```

| Variable | What it is |
|---|---|
| `KIRO_BRIDGE_API_ID` | from my.telegram.org, API development tools |
| `KIRO_BRIDGE_API_HASH` | same page |
| `KIRO_BRIDGE_PHONE` | the bridge account's number |
| `KIRO_BRIDGE_OWNER_ID` | your own Telegram user id, the only account served |
| `KIRO_API_KEY` | only if `kiro-cli` is not already logged in here |
| `DEEPSEEK_API_KEY` | only for `.deepseek` mode |
| `KIRO_BRIDGE_WEB_TOKEN` | password for the web page, if you expose it |

`.env` is gitignored. Real environment variables win over `.env`, and both win
over `config.json`, so anything set here shows up read-only in the web page.

You can skip `.env` entirely and type the same values into the web page
instead, in which case they are stored in `config.json` (also gitignored).

## First run

```
python main.py
```

Open <http://localhost:8080> and press Start. Telegram sends a login code to the
bridge account; enter it in the dialog. If that account has two-factor auth, the
cloud password is asked next. The session is saved to disk, so this happens once.

## Commands

Message the bridge account. Plain text goes straight to `kiro-cli`. Commands
start with a dot.

| Command | Effect |
|---|---|
| `.help` | list every command |
| `.kiro <text>` | same as plain text, explicit form |
| `.models` | available models |
| `.model <name>` | switch model |
| `.sessions` | saved `kiro-cli` sessions |
| `.resume <id>` | continue a session |
| `.new` | start a fresh session |
| `.effort <level>` | `low`, `medium`, `high`, `xhigh`, `max` |
| `.agents` / `.agent <name>` | list or pick an agent |
| `.mcp` / `.mcpstatus <name>` | configured MCP servers and their state |
| `.settings` / `.set <key> <value>` | read or change `kiro-cli` settings |
| `.whoami` | which kiro account is logged in |
| `.version` | `kiro-cli` version |
| `.status` | model, session, agent, effort |
| `.diag` | run diagnostics |
| `.raw <args>` | pass arguments straight to `kiro-cli` |
| `.profiles` / `.profile <name>` | saved appearance presets, see below |
| `.username <name>` | set the username, `.username -` clears it |

Sessions belong to a working directory, so `.sessions` only lists the ones for
the folder set as `kiro_working_dir`.

## Profiles

Presets for the bridge account's appearance: display name, username, bio and
photo. Create them on the web page, switch from Telegram with
`.profile <name>`. Applying a preset that carries a photo removes the previous
profile photos first, so old avatars do not pile up.

## Direct API mode

`kiro-cli` is the default backend, but you can hand messages to a provider API
instead for a while.

```
.providers                    list providers and whether a key is set
.deepseek                     enter DeepSeek mode on deepseek-v4-flash
.deepseek deepseek-v4-pro     pick the model up front
.use deepseek                 generic form
```

Inside the mode every message goes to the provider and the conversation is kept
in memory. Available there: `.model`, `.models`, `.think` to toggle thinking
mode, `.system <text>` to set a system prompt (`.system -` clears it), `.reset`
to forget the conversation, `.status`, and `.help`. Leave with `.exit` — plain
`exit`, `quit` and `выход` work too.

Adding another provider means one entry in `PROVIDERS` in `providers.py`: base
URL, the config key holding its API key, and the model names. The `.<name>`
command is picked up automatically.

## Restricting chats

By default the bridge answers you in any chat. List chat ids on the web page to
narrow it down; messages from anyone else are ignored either way.

## Layout

```
main.py              startup, wires everything together
config.py            settings, .env loading, secret handling
telegram_client.py   Telegram side: login, command dispatch
kiro_bridge.py       runs kiro-cli
providers.py         direct provider APIs used by .deepseek
profiles.py          appearance presets
web_app.py           configuration API and security middleware
templates/           the web page
```

## Security

The web page listens on `127.0.0.1` and has no password by default.

- API keys are never sent to the browser. Secret fields show up empty; leaving
  one blank keeps the stored value, entering `-` clears it.
- Requests with a foreign `Origin` or `Sec-Fetch-Site: cross-site` are refused,
  and writes must be `application/json`. Without this, any page you visited
  could quietly post to localhost, change `owner_id`, and hand command
  execution to someone else.
- Profiles cannot be written through the bulk config endpoint, and an avatar
  filename can never point outside `avatars/`.
- Uploads are limited to image extensions and 8 MB.
- Prompts reach `kiro-cli` as separate arguments with no shell in between, so
  newlines, quotes, `&` and Windows paths pass through as typed and cannot turn
  into extra commands.

To reach the page from another machine, set `KIRO_BRIDGE_WEB_TOKEN` and point
`web_host` at the interface you need. Binding anything other than loopback
without a token is refused at startup. Open it once as
`http://host:8080/?token=...` to store the cookie.

`kiro-cli` runs with `--trust-all-tools`, so whoever owns the configured
`owner_id` can run commands on this machine. Keep that id correct.

## Tests

```
python test_access.py          only the owner is served
python test_security.py        secret masking, cross-site writes, path escapes
python test_env.py             .env loading and precedence
python test_command_build.py   argument building, no shell involved
python test_kiro_command.py    .kiro routing
python test_mode.py            provider mode and .exit
```

## License

MIT
