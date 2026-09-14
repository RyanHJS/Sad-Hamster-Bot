"""A private Discord bridge to a local coding agent.

One channel is one repo folder is one agent session. Messages from the authorized
user run in that folder; tool activity streams back as embeds that update in place.
"""

import argparse
import asyncio
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

import discord

import runner_claude
import runner_codex
from sessions import Lease, Sessions
from settings import AGENTS, Settings

log = logging.getLogger("sad_hamster_bot")

RUNNERS = {runner_codex.NAME: runner_codex, runner_claude.NAME: runner_claude}
COMMANDS = frozenset({"help", "clear", "stop", "agent", "status"})

BLURPLE, GREEN, RED, GREY, AMBER = 0x5865F2, 0x57F287, 0xED4245, 0x4E5058, 0xFEE75C


def configure_logging(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f"sad-hamster-bot-{datetime.now():%Y-%m-%dT%H%M%S}-", suffix=".log", dir=directory
    )
    os.close(fd)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(name)],
        force=True,
    )
    return Path(name)


def split_message(text, limit=1900):
    """Split on line boundaries where possible; Discord rejects overlong messages."""
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks or [""]


def parse_command(text):
    """Return argv for a slash command, or None if this is a request for the agent.

    Requiring the slash means an ordinary request that happens to start with
    "status" still reaches the agent.
    """
    if not text.startswith("/"):
        return None
    parts = text[1:].split()
    if not parts or parts[0].lower() not in COMMANDS:
        return None
    return [parts[0].lower(), *(p.lower() for p in parts[1:])]


HELP = (
    "**Sad Hamster Bot**\nSend a message in a repo channel and I run it through the "
    "agent there. Commands start with `/`; everything else goes to the agent as-is.\n\n"
    "`/status` what's running · `/stop` cancel it · `/clear` forget the session and "
    "start fresh\n`/agent [codex|claude]` show or switch this channel's agent · `/help` this"
)


class AgentBot(discord.Client):
    def __init__(self, settings: Settings, sessions=None, lease=None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.lease = lease
        self.sessions = sessions or Sessions(settings.state_dir)
        self.active = {}

    def authorized(self, m):
        return (
            not m.author.bot
            and m.author.id == self.settings.user_id
            and (not self.settings.channels or m.channel.id in self.settings.channels)
            and isinstance(getattr(m.channel, "name", None), str)
        )

    async def on_ready(self):
        log.info("Ready as %s", self.user)

    def agent_for(self, channel_id):
        row = self.sessions.get(channel_id)
        return (row or {}).get("agent") or self.settings.agent

    async def on_message(self, m):
        if not self.authorized(m):
            return
        text = (m.content or "").strip()
        if not text:
            return
        try:
            command = parse_command(text)
            if command is not None:
                await self.control(m, command)
            elif m.channel.id in self.active:
                await m.channel.send("Still working here. `/status` to check, `/stop` to cancel.")
            else:
                await self.dispatch_run(m, text)
        except ValueError as exc:
            await m.channel.send(str(exc))
        except discord.HTTPException:
            log.warning("Discord rejected a reply in channel %s", m.channel.id)

    async def control(self, m, argv):
        action, rest = argv[0], argv[1:]
        channel_id = m.channel.id
        if action == "help":
            await m.channel.send(HELP)
        elif action == "status":
            run = self.active.get(channel_id)
            await m.channel.send(
                f"Running {run['agent']} in #{m.channel.name}." if run
                else f"Idle. #{m.channel.name} uses {self.agent_for(channel_id)}."
            )
        elif action == "stop":
            run = self.active.get(channel_id)
            if not run:
                await m.channel.send("Nothing is running here.")
            else:
                run["cancel"].set()
                await m.channel.send("Stopping.")
        elif action == "clear":
            self.sessions.clear(channel_id)
            await m.channel.send("Forgot this channel's session. The next message starts fresh.")
        elif action == "agent":
            await self.switch_agent(m, rest)

    async def switch_agent(self, m, rest):
        if not rest:
            await m.channel.send(f"#{m.channel.name} uses {self.agent_for(m.channel.id)}.")
            return
        choice = rest[0]
        if choice not in AGENTS:
            await m.channel.send(f"Unknown agent. Choose one of: {', '.join(AGENTS)}.")
            return
        if not self.binary_for(choice):
            await m.channel.send(f"{choice} is not installed on this machine.")
            return
        if m.channel.id in self.active:
            await m.channel.send("Finish or `/stop` the current run before switching agents.")
            return
        self.sessions.set_agent(m.channel.id, choice)
        await m.channel.send(f"#{m.channel.name} now uses {choice}. Its session was reset.")

    def binary_for(self, agent):
        return self.settings.claude_bin if agent == "claude" else self.settings.codex_bin

    async def dispatch_run(self, m, prompt):
        folder = self.settings.folder_for(m.channel.name)
        agent = self.agent_for(m.channel.id)
        if not self.binary_for(agent):
            await m.channel.send(f"{agent} is not installed on this machine.")
            return
        row = self.sessions.get(m.channel.id) or {}
        session_id = row.get("session_id")
        header = await m.channel.send(
            embed=discord.Embed(
                description=(
                    f"{'Resuming' if session_id else 'Starting'} {agent} in "
                    f"`{folder.name}`…"
                ),
                colour=BLURPLE,
            )
        )
        cancel = asyncio.Event()
        run = {"agent": agent, "cancel": cancel, "task": None}
        self.active[m.channel.id] = run
        run["task"] = asyncio.create_task(
            self.execute(m.channel, agent, folder, prompt, session_id, cancel, header)
        )

    async def execute(self, channel, agent, folder, prompt, session_id, cancel, header):
        """Run the agent, streaming events, and always release the channel."""
        pending, tools = [], {}

        def on_event(event):
            # Runners are synchronous; queue the Discord work for the pump below.
            pending.append(event)

        async def pump():
            while True:
                while pending:
                    await self.show(channel, tools, pending.pop(0))
                await asyncio.sleep(0.4)

        pumping = asyncio.create_task(pump())
        try:
            result = await RUNNERS[agent].run(
                self.settings, folder, prompt, session_id, on_event, cancel
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The class name is enough to debug; the prompt and traceback are private.
            log.error("Run failed in %s type=%s", folder.name, type(exc).__name__)
            await self.say(channel, "The run failed unexpectedly. Check the bot log.", RED)
            return
        finally:
            pumping.cancel()
            await asyncio.gather(pumping, return_exceptions=True)
            while pending:
                await self.show(channel, tools, pending.pop(0))
            self.active.pop(channel.id, None)

        if result.session_id:
            self.sessions.remember(channel.id, result.session_id, agent)
        await self.finish(channel, agent, result, header)

    async def show(self, channel, tools, event):
        """Render one event: tools get their own embed, edited when they finish."""
        try:
            if event.kind == "tool":
                message = await channel.send(
                    embed=discord.Embed(description=f"⏳ {event.value}", colour=GREY)
                )
                tools[event.key] = (message, event.value)
            elif event.kind == "result":
                entry = tools.pop(event.key, None)
                if entry:
                    message, label = entry
                    mark, colour = ("✅", GREEN) if not event.failed else ("❌", RED)
                    await message.edit(
                        embed=discord.Embed(
                            description=f"{mark} {label}\n-# {event.value}",
                            colour=colour,
                        )
                    )
            elif event.kind == "note":
                await self.say(channel, event.value, AMBER)
        except discord.HTTPException:
            log.warning("Could not render a %s event", event.kind)

    async def say(self, channel, text, colour):
        await channel.send(embed=discord.Embed(description=text, colour=colour))

    async def finish(self, channel, agent, result, header):
        facts = [agent, f"{result.elapsed:.0f}s"]
        if result.tokens is not None:
            facts.append(f"{result.tokens:,} tokens")
        if result.turns is not None:
            facts.append(f"{result.turns} turns")
        states = {"succeeded": ("Done", GREEN), "cancelled": ("Stopped", AMBER),
                  "timed_out": ("Deadline reached", AMBER)}
        title, colour = states.get(result.state, ("Failed", RED))
        try:
            await header.edit(
                embed=discord.Embed(
                    description=f"**{title}** · {' · '.join(facts)}", colour=colour
                )
            )
            for chunk in split_message(result.message, self.settings.chunk_size):
                await channel.send(chunk)
        except discord.HTTPException:
            log.warning("Could not deliver the result for a %s run", agent)

    async def close(self):
        for run in self.active.values():
            run["cancel"].set()
            if run["task"]:
                run["task"].cancel()
        tasks = [r["task"] for r in self.active.values() if r["task"]]
        await asyncio.gather(*tasks, return_exceptions=True)
        self.sessions.close()
        if self.lease is not None:
            self.lease.close()
        await super().close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--check", action="store_true", help="Validate settings and exit")
    args = parser.parse_args()
    try:
        if os.name != "posix":
            raise ValueError("Sad Hamster Bot supports macOS and Linux")
        settings = Settings.load(args.env_file)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.check:
        print(f"Configuration valid. Default agent: {settings.agent}. "
              "Discord and agent authentication were not checked.")
        return
    log_path = configure_logging(settings.log_dir)
    log.info("Starting Sad Hamster Bot log_file=%s", log_path)
    try:
        lease = Lease(settings.state_dir / "runtime.lock")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    try:
        AgentBot(settings, lease=lease).run(settings.token, log_handler=None)
    finally:
        lease.close()


if __name__ == "__main__":
    main()
