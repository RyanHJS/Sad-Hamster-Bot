# Sad Hamster Bot

A small, private Discord bot that runs your local coding agent — Codex CLI or
Claude Code — against your own repos, from Discord.

One channel is one repo is one session. A channel named `#my-repo` runs in
`BASE_FOLDER/my-repo`, and its conversation resumes automatically, so follow-ups
keep their context. Tool activity streams back as messages that update in place.

> **This bot runs an agent with your permissions, unattended.** Whoever holds the
> configured Discord account can change files and run commands in those folders,
> limited only by your local agent configuration. There is no per-command
> approval step. Run it as a private bot, on a machine you trust, in channels
> only you can post to.

## Setup

Requires macOS or Linux, Python 3.12+, and at least one installed, authenticated
agent CLI: [Codex](https://developers.openai.com/codex/cli/) (`codex login`) or
[Claude Code](https://code.claude.com/docs) (`claude`).

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env
```

Create an application in the
[Discord Developer Portal](https://discord.com/developers/applications), add a
bot, and enable **Message Content Intent**. Invite it with the `bot` scope and
**View Channels** and **Send Messages**.

Enable Developer Mode in Discord to copy your user ID. Put it and the token in
`.env`, and point `BASE_FOLDER` at the parent directory of your repos. Keep
`.env` private; Git ignores it.

## Run it

```sh
.venv/bin/sad-hamster-bot --check
.venv/bin/sad-hamster-bot
```

`--check` validates configuration and finds the executables without connecting to
Discord. Every failure names the setting that caused it.

Then create a channel named after a folder under `BASE_FOLDER` and send a message:

```text
summarize this project's README
```

## Commands

Commands start with `/`. Anything else goes to the agent verbatim, so a request
that happens to begin with "status" still reaches the agent.

| Command | Effect |
| --- | --- |
| `/status` | Whether a run is active, and which agent this channel uses |
| `/stop` | Cancel the running job, including its child tools |
| `/clear` | Forget this channel's session; the next message starts fresh |
| `/agent [codex\|claude]` | Show or switch this channel's agent (switching resets its session) |
| `/help` | The same summary, in Discord |

One run at a time per channel; further messages are refused, not queued.
Different channels are independent and can run at once.

## Configuration

Settings load from `.env` in the launch directory, or `--env-file /path/to/.env`.
Environment variables win over the file. Relative paths resolve against the file;
`~` expands.

| Setting | Default | Purpose |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | Required | Bot token |
| `DISCORD_USER_ID` | Required | The one authorized user |
| `BASE_FOLDER` | Required | Parent folder of your repos |
| `DISCORD_CHANNEL_IDS` | any | Optional channel allowlist |
| `AGENT` | `codex` | Default agent: `codex` or `claude` |
| `CODEX_BIN` / `CLAUDE_BIN` | `codex` / `claude` | Executable name or path |
| `MODEL` | agent default | Optional model override |
| `REASONING_EFFORT` | agent default | Codex only: `none`…`max` |
| `MAX_RUN_SECONDS` | `0` | Overall deadline; zero means none |
| `OUTPUT_LIMIT` | 1 MiB | Cap on a single JSONL record |
| `DISCORD_CHUNK_SIZE` | `1900` | Reply chunk size |
| `DISCORD_LOG_DIR` | `logs` | Per-launch logs |
| `BOT_STATE_DIR` | `state` | SQLite session map and lock |

Leaving `MODEL` and `REASONING_EFFORT` blank passes no model flags at all, so the
agent uses its own configuration.

Channel names are resolved strictly: a name containing `/`, `\`, or a leading `.`
is refused, and the resolved path must stay inside `BASE_FOLDER`.

One bot owns a state directory at a time, enforced by a lock file, so a second
launch exits with a clear error instead of running two agents over one folder.

## How it works

`codex exec [resume <id>] --json --skip-git-repo-check -` with the prompt on
stdin, or `claude -p <prompt> --output-format stream-json --verbose [--resume
<id>]`. Both stream JSONL, which becomes the tool and result messages you see.
Unrecognized event types are ignored rather than treated as errors, so an agent
CLI update does not break a run.

Each launch writes a timestamped log of status and errors. Prompts and responses
are never written to disk, and failures log the exception class only. `DISCORD_*`
variables are withheld from the agent's environment.

Cancellation, deadlines, and shutdown terminate the agent's whole process group
(SIGTERM, then SIGKILL), so ordinary child tools die with it. A tool that
deliberately detaches into its own process group is outside that cleanup.

## Troubleshooting

- **No reply**: check `DISCORD_USER_ID`, Message Content Intent, and that the
  channel name matches a folder under `BASE_FOLDER`.
- **"No folder for #name"**: create it, or rename the channel. Discord turns
  spaces into hyphens.
- **Startup error**: fix the named setting and rerun `--check`.
- **Agent failure**: run the agent directly in that folder to see its own error.
  Nothing is retried automatically, since a run may already have changed files.
- **Long replies** are split into chunks; a single oversized JSONL record stops
  the run at `OUTPUT_LIMIT`.

## Development

```sh
.venv/bin/python -m pip install -e '.[dev]'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
```

Seven modules: `settings` validates configuration, `sessions` owns the SQLite
channel-to-session map and the lock, `process` spawns and reaps agent processes,
`runner_codex` and `runner_claude` translate each CLI's JSONL into a shared event
shape defined in `events`, and `bot` is the Discord client.

The runner tests replay JSONL fixtures captured from real CLIs
(`tests/fixtures/`), so they fail if an agent's output stops matching. The
process tests use real subprocesses, including one that verifies a grandchild
tool does not survive cancellation. No token or network access is needed.

**The Claude runner is covered by fixture tests but has not been exercised
against live Discord usage** — its argv and event handling are verified against
`claude 2.1.266`, not against a long real session. Codex is the tested path.
