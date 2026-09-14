# Sad Hamster Bot

A small, private Discord bot that sends your requests to the local Codex CLI.
It works with any local workspace.

The bot starts and continues long-running Codex sessions from Discord. It
acknowledges work immediately, streams safe progress events, and keeps jobs and
results in private local state; see [TODO](TODO.md) for future improvements.

Send a request in the configured channel. It replies with a compact summary of
model, reasoning effort, token usage, and elapsed execution time, then the final
answer. Startup banners, echoed prompts, and tool output stay out of Discord.

## Setup

Requires macOS or Linux, Python 3.12+, and an installed, authenticated
[Codex CLI](https://developers.openai.com/codex/cli/).
Run `codex login` and confirm `codex exec --help` works before starting the bot.
The bot uses Codex's existing authentication and sandbox configuration.

From this directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env
```

Create an application in the [Discord Developer Portal](https://discord.com/developers/applications)
and add a bot. Enable **Message Content Intent** in the bot settings. Use the
OAuth2 URL Generator with the `bot` and `applications.commands` scopes and
**View Channels** and **Send Messages** permissions to invite it to your server.
The `applications.commands` scope is what registers `/help`.

In Discord, enable Developer Mode and copy your user ID and the channel ID.
Put those IDs and the bot token in `.env`. Set `CODEX_WORKDIR` to the directory
you want Codex to work in. Keep `.env` private; Git ignores it.

## Start the bot

After creating `.env` and installing the package, start the bot from this
directory with:

```sh
.venv/bin/sad-hamster-bot --check
.venv/bin/sad-hamster-bot
```

The first command validates configuration. The second command connects to
Discord and keeps the bot running; leave that terminal open. Stop it with
Ctrl-C. To use a different environment file, run
`.venv/bin/sad-hamster-bot --env-file /path/to/.env`.

`--check` validates local configuration and finds the executable without
connecting to Discord or using Codex tokens. It does not verify authentication.
In Discord, send a request in the configured channel:

```text
summarize this project's README
```

Only the configured user in the configured channel can invoke Codex, and only
one request runs at a time. Busy requests are rejected, not queued. Each request
continues the selected Codex session; use `/session new` for unrelated context.
Commands start with `/`; anything else is passed to Codex verbatim, so a request
that happens to begin with a word like "status" still reaches the agent.
Stop with Ctrl-C; restart after changing code or
configuration. Work in progress when the bot stops is reported as interrupted on
the next launch and is never replayed. The Discord display name and profile
picture are set separately in the Developer Portal.

## Configuration

Settings load from `.env` in the launch directory. Use
`sad-hamster-bot --env-file /path/to/.env` when launching elsewhere. Existing
environment variables take precedence. Relative paths resolve against the
chosen file; `~` expands to your home directory. Shell substitutions such as
`$PWD` are not expanded in the file.

| Setting | Required / Default | Purpose |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | Required | Discord bot token |
| `DISCORD_USER_ID` | Required | The one authorized user |
| `DISCORD_CHANNEL_ID` | Required | The one authorized channel |
| `CODEX_WORKDIR` | Required | Existing target workspace |
| `CODEX_BIN` | `codex` | Executable name or path, without arguments |
| `CODEX_MAX_RUN_SECONDS` | `0` | Optional overall deadline; zero permits long-running tasks |
| `CODEX_MODEL` | Codex default | Optional model override; blank passes no `--model` |
| `CODEX_REASONING_EFFORT` | Codex default | Optional effort supported by your model |
| `DISCORD_LOG_DIR` | `logs` | Directory for per-launch logs |
| `BOT_STATE_DIR` | `state` | Private SQLite job/session state |

Supported effort settings are `none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, and `max`; the selected model and installed Codex version must support
the value. Leave both blank to inherit whatever your Codex configuration
selects: the bot never chooses a model on your behalf and ships no model list of
its own. All other Codex settings remain in your Codex configuration.

The bridge invokes Codex in JSON event mode and passes the request through
stdin. Follow-ups use `codex exec resume <session-id> -`; the session ID is
stored in the private SQLite database under `BOT_STATE_DIR`. `/session new`
starts an unrelated conversation, while `/session list` and `/session resume
<id>` select saved conversations. A custom `CODEX_BIN` wrapper must preserve
JSONL stdout and separate stderr from stdout.

Mention the bot with `/status`, `/cancel`, or `/result <job-id>` while a task is
running. Requests receive an immediate acknowledgment and periodic status
updates. A zero `CODEX_MAX_RUN_SECONDS` allows long-running work; startup and
idle warnings describe what the bot has observed without killing a quiet agent.

One bot at a time owns a workspace. The state directory holds an exclusive lock
for the process lifetime, so a second launch against the same `CODEX_WORKDIR`
exits with a clear error instead of running two agents over the same files.

## Discord Help

Use `/help` for an ephemeral command reference. Mentioning the bot without a
request sends the same guide:

```text
@SadHamsterBot summarize the deployment configuration
@SadHamsterBot /status
@SadHamsterBot /cancel
@SadHamsterBot /result <job-id>
@SadHamsterBot /session new unrelated investigation
@SadHamsterBot /session list
@SadHamsterBot /session resume <session-id>
```

Every command is prefixed with `/`. A mention without a leading `/` is always
treated as a request for Codex, so `status of the migration` reaches the agent
rather than being parsed as a command.

The `/help` slash command requires the `applications.commands` scope when
inviting the bot. Mention-based requests still require Message Content Intent.

## Operation and Troubleshooting

The authorized Discord user can request whatever your local Codex configuration
allows, including workspace changes. Run this as a private bot on a trusted
machine. The bot does not change Codex's approval or sandbox settings, and does
not pass `DISCORD_*` environment variables to the Codex process.

Each launch creates a separate timestamped log with request IDs and status,
without recording prompts, responses, or raw Codex diagnostics. Logs are not
automatically rotated; remove old files as needed. Codex itself may retain
sessions according to its own configuration.

- No reply: check the configured IDs, Message Content Intent, channel
  permissions, and that you selected an actual bot mention.
- Startup configuration error: fix the named setting and rerun `--check`.
- Codex failure: check `codex login` and run Codex directly in the configured
  workspace to inspect its error. The bot does not automatically retry tasks
  because they may already have changed files.
- Timeout: `CODEX_MAX_RUN_SECONDS` is an optional overall deadline. It does not
  mean Codex failed to connect; the status reports whether agent activity was
  observed and whether server acceptance is unknown. Deadlines and shutdown
  kill Codex's process group, including ordinary child tools. Tools
  that deliberately detach into another process group are outside that cleanup.
- Output limit: each output stream is capped at 1 MiB; exceeding it stops the
  run. Ask for a smaller result. Long answers are sent in 1,900-character chunks.
- Metadata says `unavailable`: the CLI did not emit that field. The attempted
  model and effort remain visible in the job result, including failed attempts.
- Discord delivery failure: the log records the message ID and HTTP status;
  the next request can still run. There is no persistent queue or reply retry.

## Development

```sh
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
.venv/bin/python -m build
```

There are two runtime dependencies: `discord.py` and `python-dotenv`. The code is
split by responsibility: `bot_settings` validates configuration, `codex_runner`
runs one Codex process and parses its JSONL stream, `job_store` owns the SQLite
state, `job_manager` owns job execution independently of Discord, `runtime_lock`
enforces one owner per workspace, and `sad_hamster_bot` is the Discord client.
Tests exercise real local subprocesses as well as mocked Discord delivery; they
need no token or network access. CI runs tests, lint, and package builds on Linux
and macOS.
