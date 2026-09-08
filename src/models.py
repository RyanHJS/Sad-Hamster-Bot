"""Data models shared by the Discord bridge."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CodexResult:
    message: str
    elapsed: float
    model: str = "unavailable"
    reasoning: str = "unavailable"
    tokens: str = "unavailable"
    retries: int = 0

    def format(self, user_id: int | None = None) -> str:
        prefix = f"<@{user_id}>\n" if user_id is not None else ""
        return (f"{prefix}**Model:** {self.model} | **Reasoning:** {self.reasoning} | "
                f"**Tokens:** {self.tokens} | **Time:** {self.elapsed:.1f}s | **Retries:** {self.retries}\n\n{self.message}")
