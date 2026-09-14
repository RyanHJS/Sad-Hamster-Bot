"""Single-attempt Codex JSONL runner with private, bounded progress reporting."""

import asyncio
import json
import os
import signal
from dataclasses import dataclass
from time import monotonic


@dataclass
class RunResult:
    state: str
    message: str
    elapsed: float
    model: str
    reasoning: str
    tokens: int | None = None
    session_id: str | None = None


class _Stop(Exception):
    def __init__(self, state, message):
        self.state = state
        self.message = message


def _protocol():
    return _Stop("protocol_error", "Codex returned an invalid event stream.")


def _category(text):
    text = text.lower()
    if any(
        word in text
        for word in (
            "authentication",
            "unauthorized",
            "invalid api key",
            "not logged in",
            "token expired",
        )
    ):
        return "Codex reported an authentication error."
    if any(
        word in text
        for word in (
            "connection reset",
            "connection refused",
            "connection timed out",
            "network error",
            "stream disconnected",
            "transport error",
        )
    ):
        return "Codex reported a transport error."
    return None


async def _drain(stream):
    while await stream.read(8192):
        pass


async def _cleanup(process, grace):
    # The group can outlive its leader, so returncode alone is insufficient.
    async def send(sig):
        try:
            os.killpg(process.pid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Darwin can deny signals during process-group teardown. Allow the
            # child watcher to report exit, but never hide denial for a live child.
            for _ in range(10):
                if process.returncode is not None:
                    return False
                await asyncio.sleep(0.01)
            raise

    drains = [asyncio.create_task(_drain(stream)) for stream in (process.stdout, process.stderr)]
    try:
        if await send(signal.SIGTERM):
            deadline = monotonic() + max(0, grace)
            while monotonic() < deadline and await send(0):
                await asyncio.sleep(min(0.02, max(0, deadline - monotonic())))
            await send(signal.SIGKILL)
        await process.wait()
    finally:
        for task in drains:
            task.cancel()
        await asyncio.gather(*drains, return_exceptions=True)


async def run_codex(
    prompt,
    settings,
    session_id=None,
    on_event=lambda event: None,
    cancel=None,
    *,
    terminate_grace=None,
) -> RunResult:
    """Run once; callbacks are synchronous and contain only allowlisted progress.

    ``output_limit`` bounds each JSONL record and the combined assistant answer,
    not cumulative tool traffic. ``terminate_grace`` controls TERM-to-KILL delay.
    """
    start = monotonic()
    model = settings.model or "unavailable"
    reasoning = settings.reasoning or "unavailable"
    limit = getattr(settings, "output_limit", 1024 * 1024)
    if terminate_grace is None:
        terminate_grace = getattr(settings, "terminate_grace", 5.0)
    process = None
    tasks = []
    answers = []
    answer_size = 0
    explicit_final = False
    tokens = None
    completed = False
    seen = False
    stderr_tail = bytearray()

    def emit(event):
        on_event(event)

    def parse(line):
        nonlocal session_id, tokens, completed, seen, answer_size, explicit_final
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            raise _protocol() from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise _protocol()
        seen = True
        kind = event["type"]
        if kind == "thread.started":
            value = event.get("thread_id")
            if not isinstance(value, str) or not value or len(value) > 512:
                raise _protocol()
            if session_id is not None and value != session_id:
                raise _protocol()
            session_id = value
            emit({"kind": "session", "session_id": value})
        elif kind in ("item.started", "item.updated", "item.completed"):
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise _protocol()
            item_type = item["type"]
            if item_type == "agent_message":
                if kind == "item.completed" or "text" in item:
                    if not isinstance(item.get("text"), str):
                        raise _protocol()
                phase = item.get("phase")
                if phase is not None and not isinstance(phase, str):
                    raise _protocol()
                keep = phase == "final_answer" or (phase is None and not explicit_final)
                if kind == "item.completed" and keep:
                    if phase == "final_answer" and not explicit_final:
                        answers.clear()
                        answer_size = 0
                        explicit_final = True
                    text = item["text"]
                    try:
                        size = len(text.encode("utf-8")) + bool(answers)
                    except UnicodeError:
                        raise _protocol() from None
                    answer_size += size
                    if answer_size > limit:
                        raise _Stop("output_limit", "Codex output exceeded the size limit.")
                    answers.append(text)
            activity = {
                "agent_message": "Preparing response.",
                "reasoning": "Working on request.",
                "command_execution": "Running a tool.",
                "file_change": "Updating files.",
                "mcp_tool_call": "Running a tool.",
                "web_search": "Searching.",
                "todo_list": "Updating plan.",
            }.get(item_type)
            if activity:
                emit({"kind": "activity", "text": activity})
        elif kind == "turn.completed":
            usage = event.get("usage")
            if "usage" in event:
                if not isinstance(usage, dict):
                    raise _protocol()
                counts = [usage.get(key) for key in ("input_tokens", "output_tokens")]
                if any(type(value) is not int or value < 0 for value in counts):
                    raise _protocol()
                tokens = sum(counts)
            completed = True
        elif kind == "turn.failed":
            error = event.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                raise _protocol()
            raise _Stop("failed", _category(error["message"]) or "Codex reported a failed turn.")
        elif kind == "error":
            if not isinstance(event.get("message"), str):
                raise _protocol()
            emit(
                {
                    "kind": "warning",
                    "text": _category(event["message"]) or "Codex reported an intermediate error.",
                }
            )

    async def stdout():
        pending = bytearray()
        while chunk := await process.stdout.read(min(8192, limit + 1)):
            pending.extend(chunk)
            while True:
                end = pending.find(b"\n")
                if end < 0:
                    if len(pending) > limit:
                        raise _Stop("output_limit", "Codex output exceeded the size limit.")
                    break
                if end > limit:
                    raise _Stop("output_limit", "Codex output exceeded the size limit.")
                line = bytes(pending[:end])
                del pending[: end + 1]
                parse(line)
        if pending:
            parse(bytes(pending))

    async def stderr():
        while chunk := await process.stderr.read(8192):
            stderr_tail.extend(chunk)
            del stderr_tail[: -min(limit, 8192)]

    async def exited():
        # wait() may await pipe EOF from descendants even after the leader exits.
        while process.returncode is None:
            await asyncio.sleep(0.01)
        if process.returncode != 0:
            raise _Stop(
                "failed",
                _category(stderr_tail.decode("utf-8", errors="replace"))
                or "Codex exited unsuccessfully.",
            )

    async def stdin():
        try:
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def attempt():
        nonlocal process
        if cancel is not None and cancel.is_set():
            raise _Stop("cancelled", "Codex was cancelled.")
        args = [str(settings.codex_bin), "exec"]
        if session_id is not None:
            args.append("resume")
        args.extend(["--json", "--skip-git-repo-check"])
        if settings.model:
            args.extend(["--model", settings.model])
        if settings.reasoning:
            args.extend(["-c", f"model_reasoning_effort={json.dumps(settings.reasoning)}"])
        if session_id is not None:
            args.append(session_id)
        args.append("-")
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *args,
                cwd=settings.workdir,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("DISCORD_")
                },
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                pass_fds=getattr(settings, "lease_fds", ()),
            )
        )
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            raise
        emit({"kind": "started", "pid": process.pid})
        tasks.extend(asyncio.create_task(coro) for coro in (stdout(), stderr(), stdin(), exited()))
        exit_task = tasks[-1]
        drain_deadline = None
        pending = set(tasks)
        cancel_task = None
        if cancel is not None:
            cancel_task = asyncio.create_task(cancel.wait())
            tasks.append(cancel_task)
            pending.add(cancel_task)
        while pending - {cancel_task}:
            remaining = None if drain_deadline is None else max(0, drain_deadline - monotonic())
            done, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                break
            if cancel_task in done:
                raise _Stop("cancelled", "Codex was cancelled.")
            for task in done:
                task.result()
            if exit_task in done:
                # Give buffered records a brief drain window without waiting for descendants' EOF.
                drain_deadline = monotonic() + 0.2
        if process.returncode != 0:
            raise _Stop("failed", "Codex exited unsuccessfully.")
        if not seen:
            raise _protocol()
        if not completed:
            raise _Stop("failed", "Codex exited before completing the turn.")

    state, message = "succeeded", ""
    try:
        async with asyncio.timeout(settings.timeout or None):
            await attempt()
        message = "\n".join(answers)
    except TimeoutError:
        state = "timed_out"
        message = "Overall execution deadline exceeded. Server acceptance may be unknown."
    except _Stop as exc:
        state, message = exc.state, exc.message
    except OSError:
        state, message = "failed", "Codex could not start. Check its executable and workspace."
    finally:

        async def finish():
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process is not None:
                await _cleanup(process, terminate_grace)

        cleanup = asyncio.create_task(finish())
        interrupted = False
        # Repeated cancellation must not abandon children or an unreaped leader.
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        cleanup.result()
        if interrupted:
            raise asyncio.CancelledError
    return RunResult(state, message, monotonic() - start, model, reasoning, tokens, session_id)
