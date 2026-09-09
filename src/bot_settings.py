"""Validated configuration for the private Discord bridge."""

import math
import os
import shutil
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_MODELS = (
    ("gpt-6-astra", "low"),
    ("gpt-5.6-luna", "medium"),
    ("gpt-5.6-sol", "low"),
    ("gpt-5.6-terra", "low"),
    ("gpt-5.5", "low"),
)
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    user_id: int
    channel_id: int
    workdir: Path
    log_dir: Path
    codex_bin: str
    timeout: float = 0
    models: tuple[tuple[str, str], ...] = DEFAULT_MODELS
    retries: int = 0
    output_limit: int = 1024 * 1024
    chunk_size: int = 1900
    model: str = ""
    reasoning: str = ""
    state_dir: Path | None = None
    startup_warn: float = 30
    idle_warn: float = 120
    progress_interval: float = 15
    heartbeat_interval: float = 30
    lease_fds: tuple[int, ...] = field(default=(), repr=False)

    @classmethod
    def load(cls, env_file: Path):
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

        def number(key, default, zero=False):
            try:
                value = float(values.get(key, default))
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number") from None
            if not math.isfinite(value) or value < 0 or (value == 0 and not zero):
                raise ValueError(
                    f"{key} must be finite and {'nonnegative' if zero else 'positive'}"
                )
            return value

        def integer(key, default, minimum=1, maximum=None):
            value = number(key, default, zero=minimum == 0)
            if value != int(value) or value < minimum or (maximum and value > maximum):
                raise ValueError(f"{key} is outside the supported integer range")
            return int(value)

        if "CODEX_TIMEOUT_SECONDS" in values:
            if "CODEX_MAX_RUN_SECONDS" in values:
                raise ValueError("Use only CODEX_MAX_RUN_SECONDS, not both timeout settings")
            timeout = number("CODEX_TIMEOUT_SECONDS", 0)
            warnings.warn(
                "CODEX_TIMEOUT_SECONDS is deprecated and still kills work at its deadline; "
                "use CODEX_MAX_RUN_SECONDS=0 for unlimited execution.",
                stacklevel=2,
            )
        else:
            timeout = number("CODEX_MAX_RUN_SECONDS", 0, zero=True)
        workdir = path(required("CODEX_WORKDIR"))
        if not workdir.is_dir():
            raise ValueError("CODEX_WORKDIR must be an existing directory")
        executable = values.get("CODEX_BIN") or "codex"
        if "/" in executable:
            executable = str(path(executable))
        executable = shutil.which(executable)
        if not executable:
            raise ValueError("CODEX_BIN must name an installed executable")
        model = (values.get("CODEX_MODEL") or "").strip()
        effort = (values.get("CODEX_REASONING_EFFORT") or "").strip()
        if effort and effort not in EFFORTS:
            raise ValueError("CODEX_REASONING_EFFORT is invalid")
        raw_models = values.get("CODEX_MODELS")
        if raw_models and (model or effort):
            raise ValueError("Use CODEX_MODELS or legacy model/effort settings, not both")
        models = DEFAULT_MODELS
        if raw_models:
            try:
                models = tuple(
                    tuple(part.strip() for part in item.split(":", 1))
                    for item in raw_models.split(",")
                )
                if any(len(pair) != 2 or not pair[0] or pair[1] not in EFFORTS for pair in models):
                    raise ValueError
            except ValueError:
                raise ValueError("CODEX_MODELS must contain model:reasoning entries") from None
        elif model or effort:
            models = ((model, effort),)
        retries = integer("CODEX_RETRIES", 0, minimum=0)
        if retries:
            raise ValueError(
                "CODEX_RETRIES must be 0: safe pre-execution rejection evidence "
                "is not available; choose another model explicitly"
            )
        return cls(
            token=required("DISCORD_BOT_TOKEN"),
            user_id=identifier("DISCORD_USER_ID"),
            channel_id=identifier("DISCORD_CHANNEL_ID"),
            workdir=workdir,
            log_dir=path(values.get("DISCORD_LOG_DIR") or "logs"),
            codex_bin=executable,
            timeout=timeout,
            models=models,
            retries=retries,
            output_limit=integer("CODEX_OUTPUT_LIMIT", 1024 * 1024),
            chunk_size=integer("DISCORD_CHUNK_SIZE", 1900, minimum=128, maximum=1900),
            state_dir=path(values.get("BOT_STATE_DIR") or "state"),
            startup_warn=number("CODEX_STARTUP_WARN_SECONDS", 30),
            idle_warn=number("CODEX_IDLE_WARN_SECONDS", 120),
            progress_interval=number("DISCORD_PROGRESS_SECONDS", 15),
            heartbeat_interval=number("DISCORD_HEARTBEAT_SECONDS", 30),
        )
