"""A private Discord bridge to the Codex CLI."""

import argparse
import asyncio
import logging
import math
import os
import re
import shutil
import signal
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic

import discord
from dotenv import dotenv_values

log = logging.getLogger("sad_hamster_bot")


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    user_id: int
    channel_id: int
    workdir: Path
    log_dir: Path
    codex_bin: str
    timeout: float = 120
    model: str = ""
    reasoning: str = ""

    @classmethod
    def load(cls, env_file: Path) -> "Settings":
        env_file = env_file.expanduser().resolve()
        values = {**dotenv_values(env_file, interpolate=False), **os.environ}

        def required(key):
            value = (values.get(key) or "").strip()
            if not value:
                raise ValueError(f"{key} is required")
            return value

        def identifier(key):
            value = required(key)
            if not value.isascii() or not value.isdecimal() or int(value) <= 0:
                raise ValueError(f"{key} must be a positive Discord ID")
            return int(value)

        def path(value):
            return (env_file.parent / Path(value).expanduser()).resolve()

        try:
            timeout = float(values.get("CODEX_TIMEOUT_SECONDS", "120"))
        except (TypeError, ValueError):
            raise ValueError("CODEX_TIMEOUT_SECONDS must be a number") from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("CODEX_TIMEOUT_SECONDS must be finite and greater than zero")
        workdir = path(required("CODEX_WORKDIR"))
        if not workdir.is_dir():
            raise ValueError("CODEX_WORKDIR must be an existing directory")
        executable = values.get("CODEX_BIN", "codex") or "codex"
        if "/" in executable:
            executable = str(path(executable))
        executable = shutil.which(executable)
        if not executable:
            raise ValueError("CODEX_BIN must name an installed executable")
        reasoning = values.get("CODEX_REASONING_EFFORT") or ""
        if reasoning not in ("", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
            raise ValueError("CODEX_REASONING_EFFORT is not a recognized effort level")
        return cls(
            required("DISCORD_BOT_TOKEN"),
            identifier("DISCORD_USER_ID"),
            identifier("DISCORD_CHANNEL_ID"),
            workdir,
            path(values.get("DISCORD_LOG_DIR") or "logs"),
            executable,
            timeout,
            values.get("CODEX_MODEL") or "",
            reasoning,
        )


def configure_logging(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    prefix = f"sad-hamster-bot-{datetime.now():%Y-%m-%dT%H%M%S}-"
    descriptor, filename = tempfile.mkstemp(prefix=prefix, suffix=".log", dir=directory)
    os.close(descriptor)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(filename, encoding="utf-8")],
        force=True,
    )
    return Path(filename)


@dataclass(frozen=True)
class CodexResult:
    message: str
    elapsed: float
    model: str = "unavailable"
    reasoning: str = "unavailable"
    tokens: str = "unavailable"

    def format(self, user_id: int) -> str:
        return (
            f"<@{user_id}>\n**Model:** {self.model} | **Reasoning:** {self.reasoning} | "
            f"**Tokens:** {self.tokens} | **Time:** {self.elapsed:.1f}s\n\n{self.message}"
        )


class OutputLimitExceeded(Exception):
    pass


async def read_output(stream: asyncio.StreamReader, limit: int = 1024 * 1024) -> str:
    output = bytearray()
    while chunk := await stream.read(65536):
        output.extend(chunk)
        if len(output) > limit:
            raise OutputLimitExceeded
    return output.decode("utf-8", errors="replace").strip()


async def run_codex(prompt: str, settings: Settings) -> CodexResult:
    started = monotonic()
    command = [settings.codex_bin, "exec", "--skip-git-repo-check", "--color", "never"]
    if settings.model:
        command.extend(["--model", settings.model])
    if settings.reasoning:
        command.extend(["-c", f'model_reasoning_effort="{settings.reasoning}"'])
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            "-",
            cwd=settings.workdir,
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={key: value for key, value in os.environ.items() if not key.startswith("DISCORD_")},
        )
    except OSError:
        log.error("Could not start Codex; check CODEX_BIN and CODEX_WORKDIR")
        return CodexResult(
            "Codex could not start. Check its executable and workspace.", monotonic() - started
        )
    tasks = [
        asyncio.create_task(read_output(stream)) for stream in (process.stdout, process.stderr)
    ]
    tasks.append(asyncio.create_task(process.wait()))
    try:
        process.stdin.write(prompt.encode("utf-8"))
        process.stdin.close()
        output, diagnostics, _ = await asyncio.wait_for(asyncio.gather(*tasks), settings.timeout)
    except TimeoutError:
        return CodexResult("Codex timed out.", monotonic() - started)
    except OutputLimitExceeded:
        return CodexResult(
            "Codex stopped because its output exceeded 1 MiB.", monotonic() - started
        )
    finally:
        # Kill the session's process group so timeouts and shutdown also stop child tools.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await process.communicate()
    if process.returncode:
        log.warning("Codex exited with status %d", process.returncode)
        output = (
            f"Codex exited with status {process.returncode}. Check Codex login and configuration."
        )
    # The header precedes the echoed prompt; only the final footer contains token usage.
    header = diagnostics.split("\nuser\n", 1)[0]
    model = re.search(r"^model: (.+)$", header, re.MULTILINE)
    reasoning = re.search(r"^reasoning effort: (.+)$", header, re.MULTILINE)
    tokens = re.search(r"(?:^|\n)tokens used\s*\n([0-9][0-9,]*)\s*$", diagnostics)
    log.info("Codex finished status=%d", process.returncode)
    return CodexResult(
        output or "Codex completed without output.",
        monotonic() - started,
        model.group(1).strip() if model else "unavailable",
        reasoning.group(1).strip() if reasoning else "unavailable",
        f"{int(tokens.group(1).replace(',', '')):,}" if tokens else "unavailable",
    )


class CodexBot(discord.Client):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.lock = asyncio.Lock()

    async def on_ready(self):
        log.info("Ready as %s in channel %d", self.user, self.settings.channel_id)

    async def on_message(self, message: discord.Message):
        if (
            message.author.bot
            or message.author.id != self.settings.user_id
            or message.channel.id != self.settings.channel_id
            or self.user is None
            or not any(user.id == self.user.id for user in message.mentions)
        ):
            return
        prompt = re.sub(rf"<@!?{self.user.id}>", "", message.content).strip()
        try:
            if not prompt:
                await message.channel.send("Please include a request after mentioning me.")
                return
            if self.lock.locked():
                await message.channel.send("Codex is already working on another request.")
                return
            async with self.lock:
                log.info("Starting request message_id=%d", message.id)
                try:
                    async with message.channel.typing():
                        result = await run_codex(prompt, self.settings)
                except Exception as exc:
                    log.error(
                        "Request failed message_id=%d type=%s", message.id, type(exc).__name__
                    )
                    result = CodexResult("The request failed unexpectedly. Check the bot logs.", 0)
                text = result.format(message.author.id)
                for start in range(0, len(text), 1900):
                    await message.channel.send(
                        text[start : start + 1900],
                        allowed_mentions=discord.AllowedMentions(
                            users=[message.author] if start == 0 else False,
                            roles=False,
                            everyone=False,
                            replied_user=False,
                        ),
                    )
        except discord.HTTPException as exc:
            log.warning("Discord delivery failed message_id=%d status=%d", message.id, exc.status)


def main() -> None:
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
    CodexBot(settings).run(settings.token, log_handler=None)


if __name__ == "__main__":
    main()
