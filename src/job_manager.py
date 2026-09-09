"""Own execution independently of Discord delivery."""

import asyncio
import sqlite3
from dataclasses import replace
from time import monotonic

from codex_runner import run_codex

ACTIVE = {"accepted", "starting", "running", "cancelling"}


class JobManager:
    def __init__(self, settings, store, runner=run_codex):
        self.settings = settings
        self.store = store
        self.runner = runner
        self.scope = f"{settings.user_id}:{settings.channel_id}:{settings.workdir.resolve()}"
        self.workspace = str(settings.workdir.resolve())
        self.cancel_events = {}
        self.live = {}

    def job(self, job_id=None):
        job = self.store.get_job(self.scope, job_id)
        if job is None:
            raise ValueError("Job not found in this conversation.")
        return job

    def ensure_idle(self):
        active = self.store.active_job(self.workspace)
        if active:
            raise ValueError(f"Already working on job {active['id']}. Use status or cancel.")

    def new_session(self):
        self.ensure_idle()
        return self.store.new_session(self.scope)

    def select_session(self, session_id):
        self.ensure_idle()
        return self.store.select_session(self.scope, session_id)

    def admit(self, message_id, fresh=False):
        self.ensure_idle()
        session = None if fresh else self.store.current_session(self.scope)
        session = session or self.store.new_session(self.scope)
        try:
            job = self.store.create_job(self.scope, self.workspace, message_id, session["id"])
        except sqlite3.IntegrityError:
            raise ValueError(
                "This request was already accepted, or the workspace is busy."
            ) from None
        job["codex_id"] = session["codex_id"]
        self.cancel_events[job["id"]] = asyncio.Event()
        return job

    def cancel(self, job_id=None):
        job = self.job(job_id)
        event = self.cancel_events.get(job["id"])
        if job["state"] not in ACTIVE or event is None:
            return f"Job {job['id']} is {job['state']}; no active process to cancel."
        event.set()
        self.store.update_job(job["id"], state="cancelling")
        return f"Cancelling job {job['id']}."

    def status(self, job_id=None):
        job = self.job(job_id)
        live = self.live.get(job["id"])
        elapsed = monotonic() - live["started"] if live else (job.get("elapsed") or 0)
        lines = [f"Job {job['id']} | session {job['session_id']} | {job['state']} | {elapsed:.0f}s"]
        attempts = self.store.attempts(job["id"])
        if attempts:
            attempt = attempts[-1]
            lines.append(
                f"Model: {attempt['model'] or 'Codex default'} / "
                f"{attempt['reasoning'] or 'default effort'}"
            )
        if live:
            if live["activity_at"] is None:
                lines.append("Agent response not yet observed; server acceptance unknown.")
                if elapsed >= self.settings.startup_warn:
                    lines.append("Startup is taking longer than expected; still monitoring.")
            else:
                quiet = monotonic() - live["activity_at"]
                lines.append(f"Last agent/tool activity {quiet:.0f}s ago: {live['activity']}")
                if quiet >= self.settings.idle_warn:
                    lines.append("Quiet; process has not finished. You can cancel this job.")
            if live["warning"]:
                lines.append(live["warning"])
        elif job.get("activity"):
            lines.append(job["activity"])
        return "\n".join(lines)

    async def execute(self, job, prompt, model_index=0):
        job_id = job["id"]
        started = monotonic()
        model, reasoning = self.settings.models[model_index]
        settings = replace(self.settings, model=model, reasoning=reasoning)
        attempt = self.store.add_attempt(job_id, model, reasoning)
        live = {"started": started, "activity_at": None, "activity": "", "warning": ""}
        self.live[job_id] = live
        cancel = self.cancel_events[job_id]
        self.store.update_job(job_id, state="starting")

        def on_event(event):
            kind = event["kind"]
            if kind == "started":
                self.store.update_job(
                    job_id,
                    state="cancelling" if cancel.is_set() else "running",
                    pid=event["pid"],
                    activity="CLI started.",
                )
            elif kind == "session":
                self.store.set_codex_id(job["session_id"], event["session_id"])
            elif kind == "activity":
                live["activity_at"] = monotonic()
                live["activity"] = event["text"]
                live["warning"] = ""
                self.store.update_job(job_id, activity=event["text"])
            elif kind == "warning":
                live["warning"] = event["text"]

        state, message, tokens = "failed", "Unexpected execution failure. Check bot logs.", None
        try:
            result = await self.runner(
                prompt, settings, session_id=job.get("codex_id"), on_event=on_event, cancel=cancel
            )
            state, message, tokens = result.state, result.message, result.tokens
        except asyncio.CancelledError:
            state, message = (
                "interrupted",
                "Bot stopped. Execution interrupted; no automatic replay.",
            )
            raise
        except Exception:
            # Neither prompt contents nor raw exception diagnostics belong in Discord or logs.
            state = "failed"
        finally:
            elapsed = monotonic() - started
            self.store.update_attempt(
                attempt["id"], outcome=state, detail=message[:1000], elapsed=elapsed, tokens=tokens
            )
            terminal = (
                state
                if state in {"succeeded", "cancelled", "timed_out", "interrupted"}
                else "failed"
            )
            summary = (
                f"Job {job_id} | session {job['session_id']} | {terminal}\n"
                f"Model: {model or 'Codex default'} / {reasoning or 'default effort'}\n"
                f"Attempt 1: {state} | {elapsed:.1f}s | "
                f"Tokens: {tokens if tokens is not None else 'unknown'}\n\n{message}"
            )
            if terminal != "succeeded":
                summary += "\n\nNo automatic replay. Use a follow-up to continue or session new for fresh context."
            self.store.update_job(
                job_id,
                state=terminal,
                result=summary,
                elapsed=elapsed,
                delivery="pending",
                pid=None,
            )
            self.live.pop(job_id, None)
            self.cancel_events.pop(job_id, None)
