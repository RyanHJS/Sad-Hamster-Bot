"""The contract every agent runner speaks, so the Discord layer stays agent-agnostic."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    """One thing worth showing the user.

    ``kind`` is one of:

    ``session``  the agent reported its session id (``value``)
    ``text``     assistant prose (``value``)
    ``tool``     a tool call started; ``key`` identifies it for a later ``result``
    ``result``   that tool finished; ``value`` is a one-line summary
    ``note``     an intermediate warning worth surfacing but not fatal
    """

    kind: str
    value: str = ""
    key: str = ""
    failed: bool = False


@dataclass(frozen=True)
class RunResult:
    """How a run ended. ``state`` is succeeded, cancelled, timed_out, or failed."""

    state: str
    message: str
    elapsed: float
    session_id: str | None = None
    tokens: int | None = None
    turns: int | None = None

    @property
    def ok(self) -> bool:
        return self.state == "succeeded"
