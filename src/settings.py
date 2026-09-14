"""Validated configuration. Every failure names the setting that caused it."""

import math
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

AGENTS = ("codex", "claude")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    user_id: int
    base_folder: Path
    log_dir: Path
    state_dir: Path
    codex_bin: str = "codex"
    claude_bin: str = "claude"
    agent: str = "codex"
    channels: frozenset[int] = frozenset()
    timeout: float = 0
    output_limit: int = 1024 * 1024
    chunk_size: int = 1900
    model: str = ""
    reasoning: str = ""

    def folder_for(self, channel_name: str) -> Path:
        """Resolve a channel name to its repo folder, refusing to escape the base.

        Discord names are user-controlled, so this is a security boundary rather
        than a convenience: "../etc" must not resolve outside base_folder.
        """
        name = (channel_name or "").strip()
        if not name or name.startswith(".") or "/" in name or "\\" in name:
            raise ValueError(f"Channel name is not a usable folder name: {channel_name!r}")
        folder = (self.base_folder / name).resolve()
        if not folder.is_relative_to(self.base_folder):
            raise ValueError(f"Channel #{name} resolves outside the base folder.")
        if not folder.is_dir():
            raise ValueError(f"No folder for #{name}. Expected {folder}.")
        return folder

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

        def number(key, default, zero=True):
            try:
                value = float(values.get(key) or default)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number") from None
            if not math.isfinite(value) or value < 0 or (value == 0 and not zero):
                raise ValueError(f"{key} must be finite and nonnegative")
            return value

        def executable(key, default):
            name = (values.get(key) or "").strip() or default
            found = shutil.which(str(path(name)) if "/" in name else name)
            return found or ""

        base = path(required("BASE_FOLDER"))
        if not base.is_dir():
            raise ValueError("BASE_FOLDER must be an existing directory")

        agent = (values.get("AGENT") or "codex").strip().lower()
        if agent not in AGENTS:
            raise ValueError(f"AGENT must be one of {', '.join(AGENTS)}")

        codex_bin = executable("CODEX_BIN", "codex")
        claude_bin = executable("CLAUDE_BIN", "claude")
        # Only the default agent must be installed; the other is optional until used.
        if agent == "codex" and not codex_bin:
            raise ValueError("CODEX_BIN must name an installed executable")
        if agent == "claude" and not claude_bin:
            raise ValueError("CLAUDE_BIN must name an installed executable")

        effort = (values.get("REASONING_EFFORT") or "").strip()
        if effort and effort not in EFFORTS:
            raise ValueError(f"REASONING_EFFORT must be one of {', '.join(EFFORTS)}")

        channels = frozenset()
        if raw := (values.get("DISCORD_CHANNEL_IDS") or "").strip():
            for part in raw.replace(",", " ").split():
                if not part.isascii() or not part.isdecimal() or int(part) <= 0:
                    raise ValueError("DISCORD_CHANNEL_IDS must be positive Discord IDs")
                channels |= {int(part)}

        return cls(
            token=required("DISCORD_BOT_TOKEN"),
            user_id=identifier("DISCORD_USER_ID"),
            base_folder=base,
            log_dir=path(values.get("DISCORD_LOG_DIR") or "logs"),
            state_dir=path(values.get("BOT_STATE_DIR") or "state"),
            codex_bin=codex_bin,
            claude_bin=claude_bin,
            agent=agent,
            channels=channels,
            timeout=number("MAX_RUN_SECONDS", 0),
            output_limit=int(number("OUTPUT_LIMIT", 1024 * 1024, zero=False)),
            chunk_size=int(number("DISCORD_CHUNK_SIZE", 1900, zero=False)),
            model=(values.get("MODEL") or "").strip(),
            reasoning=effort,
        )
