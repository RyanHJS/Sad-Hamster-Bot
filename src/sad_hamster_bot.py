"""A private Discord bridge for durable Codex jobs."""

import argparse
import asyncio
import logging
import os
import re
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands

from bot_settings import Settings
from codex_runner import run_codex
from job_manager import JobManager
from job_store import JobStore
from runtime_lock import RuntimeLease

log = logging.getLogger("sad_hamster_bot")

COMMANDS = frozenset({"help", "status", "cancel", "result", "session"})


def parse_command(prompt):
    """Return argv for a slash-prefixed control command, or None for a Codex request.

    Requiring the slash keeps ordinary requests that merely begin with a command
    word ("status of the migration") out of the control path.
    """
    if not prompt.startswith("/"):
        return None
    parts = prompt[1:].split()
    if not parts or parts[0].lower() not in COMMANDS:
        return None
    verb = parts[0].lower()
    # Lowercase the verb, and the subcommand for "/session NEW". Every other
    # token is an ID or request text, so it stays verbatim.
    if verb == "session" and len(parts) > 1:
        return [verb, parts[1].lower(), *parts[2:]]
    return [verb, *parts[1:]]


def command_tail(prompt, words):
    """Return the text after the first ``words`` tokens, whitespace intact.

    Rebuilding a request by joining argv would collapse newlines and runs of
    spaces, so a multi-line prompt must be recovered from the original text.
    """
    parts = prompt.split(maxsplit=words)
    return parts[words] if len(parts) > words else ""


def state_directory(settings: Settings) -> Path:
    """Resolve the state directory once: the lease and the store must agree."""
    return settings.state_dir or settings.workdir / "state"


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
    def __init__(self, settings: Settings, store=None, runner=run_codex, lease=None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.lease = lease
        self.store = store or JobStore(state_directory(settings))
        interrupted = self.store.reconcile_interrupted()
        if interrupted:
            log.warning("Recovered %d job(s) abandoned by a previous run", interrupted)
        self.manager = JobManager(settings, self.store, runner=runner)
        self.jobs = {}
        self.status_messages = {}
        self._heartbeat = None
        self._synced = False
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
        # Start delivery before syncing: a rate-limited command sync must never keep
        # finished jobs from reaching Discord.
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.create_task(self._status_loop())
        if not self._synced:
            try:
                await self.tree.sync()
                self._synced = True
            except discord.HTTPException as exc:
                log.warning("Slash command sync failed (status=%s); /help may be stale", exc.status)

    @staticmethod
    def help_text():
        return ("**Sad Hamster Bot**\nMention me with a request to run Codex remotely. "
                "Commands start with `/`; anything else is sent to Codex as-is.\n\n"
                "`/session new [request]` starts a fresh conversation, `/session list` lists "
                "sessions, and `/session resume <id>` continues an old one.\n\n"
                "`/status [job-id]` shows activity, `/cancel [job-id]` stops a job, and "
                "`/result <job-id>` retrieves a result. Normal requests continue the selected "
                "session. Fresh sessions do not undo workspace changes.")

    async def _status_loop(self):
        while not self.is_closed():
            await asyncio.sleep(self.settings.heartbeat_interval)
            # One bad tick must never end the loop: it owns result delivery for the
            # whole process, so dying here would silently strand finished jobs.
            try:
                await self.deliver_once()
                for job_id, task in list(self.jobs.items()):
                    message = self.status_messages.get(job_id)
                    if message and not task.done():
                        try:
                            await message.edit(content=self.manager.status(job_id))
                        except discord.HTTPException:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Heartbeat tick failed; continuing")

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
        finished = [i for i, t in self.jobs.items() if t.done()]
        for job_id in finished:
            del self.jobs[job_id]
            # Drop the cached Message too, or every completed job leaks one.
            self.status_messages.pop(job_id, None)

    async def _run(self, job, prompt):
        await self.manager.execute(job, prompt)

    async def _start(self, m, prompt, fresh=False):
        """Admit a job, acknowledge it, and register it for heartbeat status edits."""
        existing = self.store.current_session(self.manager.scope)
        job = self.manager.admit(m.id, fresh=fresh)
        if fresh:
            selected = "new session created"
        else:
            selected = "session resumed" if existing else "session created"
        status = await self._send(
            m.channel, f"Accepted job {job['id']}; {selected}.", m.author
        )
        self.store.update_job(job["id"], status_message_id=status.id)
        self.status_messages[job["id"]] = status
        self.jobs[job["id"]] = asyncio.create_task(self._run(job, prompt))

    async def _control(self, m, p, prompt=""):
        action = p[0]
        try:
            if action == "help":
                out = self.help_text()
            elif action == "status":
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
                        # Route through _start so the job gets acknowledged and tracked.
                        # Take the tail from the raw text to preserve line breaks.
                        await self._start(m, command_tail(prompt, 2), fresh=True)
                        return
                    out = f"Created and selected new session {self.manager.new_session()['id']}."
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
                    out = "Usage: /session new [request], /session list, /session resume <id>"
            else:
                out = "Usage: /status [job-id], /cancel [job-id], /result <job-id>, /session ..."
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
        try:
            command = parse_command(prompt)
            if command is not None:
                # Pass the raw text too: "/session new <request>" must keep the
                # request's original line breaks and spacing.
                await self._control(m, command, prompt[1:])
                return
            await self._start(m, prompt)
        except ValueError as exc:
            await self._send(m.channel, str(exc))
        except discord.HTTPException:
            # The ack never reached Discord. Release the admitted job so the
            # workspace does not stay wedged behind a job with no process.
            job = self.store.get_job_by_message(self.manager.scope, m.id)
            if job:
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
        if self.lease is not None:
            self.lease.close()
        await super().close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--check", action="store_true", help="Validate settings without connecting")
    args = parser.parse_args()
    try:
        if os.name != "posix":
            raise ValueError("Sad Hamster Bot currently supports macOS and Linux")
        settings = Settings.load(args.env_file)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.check:
        print("Configuration valid. Discord and Codex authentication have not been checked.")
        return
    log_path = configure_logging(settings.log_dir)
    log.info("Starting Sad Hamster Bot log_file=%s", log_path)
    state_dir = state_directory(settings)
    try:
        # Held for the process lifetime and inherited by Codex, so a second bot
        # cannot drive the same workspace concurrently.
        lease = RuntimeLease(state_dir / "runtime.lock")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    settings = replace(settings, lease_fds=(lease.fd,))
    try:
        CodexBot(settings, lease=lease).run(settings.token, log_handler=None)
    finally:
        lease.close()


if __name__ == "__main__":
    main()
