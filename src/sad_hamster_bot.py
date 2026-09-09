"""A private Discord bridge for durable Codex jobs."""

import argparse
import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands

from bot_settings import Settings
from codex_runner import run_codex
from job_manager import JobManager
from job_store import JobStore

log = logging.getLogger("sad_hamster_bot")


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


class CodexBot(discord.Client):
    def __init__(self, settings: Settings, store=None, runner=run_codex):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.store = store or JobStore(settings.state_dir or settings.workdir / "state")
        self.manager = JobManager(settings, self.store, runner=runner)
        self.jobs = {}
        self.status_messages = {}
        self._heartbeat = None
        self.tree = app_commands.CommandTree(self)

        @self.tree.command(name="help", description="Show Sad Hamster Bot commands")
        async def help_command(interaction: discord.Interaction):
            await interaction.response.send_message(self.help_text(), ephemeral=True)

    def _ok(self, m):
        return (
            not m.author.bot
            and m.author.id == self.settings.user_id
            and m.channel.id == self.settings.channel_id
            and self.user
            and any(x.id == self.user.id for x in m.mentions)
        )

    async def _send(self, c, text, author=None):
        return await c.send(
            text,
            allowed_mentions=discord.AllowedMentions(
                users=[author] if author else False, roles=False, everyone=False, replied_user=False
            ),
        )

    async def on_ready(self):
        log.info("Ready as %s", self.user)
        await self.tree.sync()
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.create_task(self._status_loop())

    @staticmethod
    def help_text():
        return ("**Sad Hamster Bot**\nMention me with a request to run Codex remotely. "
                "Use `session new` for a fresh conversation, `session list` to list "
                "sessions, and `session resume <id>` to continue an old one.\n\n"
                "Use `status [job-id]` to see activity, `cancel [job-id]` to stop a job, "
                "and `result <job-id>` to retrieve a result. Normal requests continue "
                "the selected session. Fresh sessions do not undo workspace changes.")

    async def _status_loop(self):
        while not self.is_closed():
            await asyncio.sleep(self.settings.heartbeat_interval)
            await self.deliver_once()
            for job_id, task in list(self.jobs.items()):
                message = self.status_messages.get(job_id)
                if message and not task.done():
                    try:
                        await message.edit(content=self.manager.status(job_id))
                    except discord.HTTPException:
                        pass

    async def _deliver(self, job):
        if not job.get("result"):
            return
        channel = self.get_channel(self.settings.channel_id)
        if channel is None:
            return
        try:
            sent = self.store.delivered_chunks(job["id"])
            for i, chunk in enumerate(split_message(job["result"], self.settings.chunk_size)):
                if i in sent:
                    continue
                msg = await self._send(
                    channel, (f"<@{self.settings.user_id}>\n" if i == 0 else "") + chunk
                )
                self.store.record_chunk(job["id"], i, msg.id)
            self.store.update_job(job["id"], delivery="delivered")
        except discord.HTTPException as exc:
            self.store.update_job(
                job["id"], delivery="blocked" if exc.status in (403, 404) else "retry"
            )

    async def deliver_once(self):
        for job in self.store.pending_deliveries(self.manager.scope):
            await self._deliver(job)
        self.jobs = {i: t for i, t in self.jobs.items() if not t.done()}

    async def _run(self, job, prompt):
        await self.manager.execute(job, prompt)

    async def _control(self, m, prompt):
        p = prompt.split()
        action = p[0].lower() if p else ""
        try:
            if action == "status":
                out = self.manager.status(p[1] if len(p) > 1 else None)
            elif action == "cancel":
                out = self.manager.cancel(p[1] if len(p) > 1 else None)
            elif action == "result":
                out = (
                    self.manager.job(p[1] if len(p) > 1 else None).get("result")
                    or "That job has no result yet."
                )
            elif action == "session" and len(p) > 1:
                if p[1] == "new":
                    if len(p) > 2:
                        job = self.manager.admit(m.id, True)
                        out = f"Accepted job {job['id']}; new session created."
                        self.jobs[job["id"]] = asyncio.create_task(self._run(job, " ".join(p[2:])))
                    else:
                        out = (
                            f"Created and selected new session {self.manager.new_session()['id']}."
                        )
                elif p[1] == "list":
                    out = (
                        "\n".join(
                            f"{s['id']}: {s['label'] or 'unnamed'}"
                            for s in self.store.sessions(self.manager.scope)
                        )
                        or "No sessions."
                    )
                elif p[1] == "resume" and len(p) == 3:
                    out = f"Selected session {self.manager.select_session(p[2])['id']}."
                else:
                    out = "Usage: session new [request], session list, session resume <id>"
            else:
                out = "Usage: status [job-id], cancel [job-id], result <job-id>, session ..."
            await self._send(m.channel, out)
        except ValueError as exc:
            await self._send(m.channel, str(exc))

    async def on_message(self, m):
        if not self._ok(m):
            return
        prompt = re.sub(rf"<@!?{self.user.id}>", "", m.content).strip()
        if not prompt:
            await self._send(m.channel, self.help_text())
            return
        if prompt.split()[0].lower() in {"status", "cancel", "result", "session"}:
            await self._control(m, prompt)
            return
        try:
            job = self.manager.admit(m.id)
            status = await self._send(
                m.channel, f"Accepted job {job['id']}; session created or resumed.", m.author
            )
            self.store.update_job(job["id"], status_message_id=status.id)
            self.status_messages[job["id"]] = status
            self.jobs[job["id"]] = asyncio.create_task(self._run(job, prompt))
        except ValueError as exc:
            await self._send(m.channel, str(exc))
        except discord.HTTPException:
            job = self.store.get_job(self.manager.scope)
            if job and job["source_message_id"] == m.id:
                self.store.update_job(
                    job["id"],
                    state="failed",
                    result="Request was not started because acknowledgment failed.",
                    delivery="blocked",
                )

    async def close(self):
        if self._heartbeat:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
        for task in self.jobs.values():
            task.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        self.store.close()
        await super().close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    settings = Settings.load(args.env_file)
    if args.check:
        print("Configuration valid. Discord and Codex authentication have not been checked.")
        return
    configure_logging(settings.log_dir)
    CodexBot(settings).run(settings.token, log_handler=None)


if __name__ == "__main__":
    main()
