"""Run one agent CLI, stream its JSONL, and never leave child tools behind.

The cleanup here is the reason this module exists. Agent CLIs spawn child tools,
so signalling only the leader orphans them. Everything runs in its own process
group and the whole group is torn down.
"""

import asyncio
import json
import logging
import os
import signal
from time import monotonic

log = logging.getLogger("sad_hamster_bot.process")


async def _drain(stream):
    while await stream.read(8192):
        pass


async def terminate(process, grace=5.0):
    """Stop the process group, escalating TERM to KILL after ``grace`` seconds."""

    async def send(sig):
        try:
            os.killpg(process.pid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Darwin can deny signals mid-teardown. Let the child watcher report
            # exit, but never hide a denial for a process that is still alive.
            for _ in range(10):
                if process.returncode is not None:
                    return False
                await asyncio.sleep(0.01)
            raise

    # Keep reading, or the child can block on a full pipe and never exit.
    drains = [asyncio.create_task(_drain(s)) for s in (process.stdout, process.stderr)]
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


async def _pump(stream, limit, on_record):
    """Split stdout into lines and hand each parsed JSON record to ``on_record``.

    Unparseable lines are skipped rather than fatal: these CLIs also print the
    occasional non-JSON line, and one stray line should not fail a whole run.
    """
    pending = bytearray()
    while chunk := await stream.read(65536):
        pending.extend(chunk)
        while (end := pending.find(b"\n")) >= 0:
            line = bytes(pending[:end])
            del pending[: end + 1]
            _record(line, on_record)
        if len(pending) > limit:
            raise OutputLimit
    if pending:
        _record(bytes(pending), on_record)


def _record(line, on_record):
    if not line.strip():
        return
    try:
        record = json.loads(line)
    except (ValueError, UnicodeError):
        log.debug("Skipping non-JSON output line")
        return
    if isinstance(record, dict):
        on_record(record)


class OutputLimit(Exception):
    """A single JSONL record grew past the configured limit."""


async def stream_jsonl(argv, cwd, on_record, *, timeout=0, limit=1024 * 1024,
                       cancel=None, prompt=None, grace=5.0, extra_fds=()):
    """Spawn ``argv`` in ``cwd`` and feed each JSON record to ``on_record``.

    Returns ``(state, detail)`` where state is succeeded, cancelled, timed_out,
    or failed. ``on_record`` is synchronous and must not raise.
    """
    process = None
    tasks = []
    stderr_tail = bytearray()

    async def watch_stderr():
        while chunk := await process.stderr.read(8192):
            stderr_tail.extend(chunk)
            del stderr_tail[:-4096]

    async def feed_stdin():
        try:
            if prompt is not None:
                process.stdin.write(prompt.encode("utf-8"))
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def attempt():
        nonlocal process
        if cancel is not None and cancel.is_set():
            return "cancelled", "Cancelled before the agent started."
        # DISCORD_* stays out of the agent's environment: it holds the bot token.
        env = {k: v for k, v in os.environ.items() if not k.startswith("DISCORD_")}
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                pass_fds=tuple(extra_fds),
            )
        )
        try:
            # Shielded so a cancel mid-spawn still yields a process to clean up.
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            raise
        tasks.extend(
            asyncio.create_task(c)
            for c in (_pump(process.stdout, limit, on_record), watch_stderr(), feed_stdin())
        )
        waiter = asyncio.create_task(process.wait())
        tasks.append(waiter)
        pending = set(tasks)
        stopper = None
        if cancel is not None:
            stopper = asyncio.create_task(cancel.wait())
            tasks.append(stopper)
            pending.add(stopper)
        while pending - {stopper}:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if stopper in done:
                return "cancelled", "Cancelled."
            for task in done:
                task.result()
        if process.returncode:
            detail = stderr_tail.decode("utf-8", errors="replace").strip()
            return "failed", detail.splitlines()[-1] if detail else "The agent exited unsuccessfully."
        return "succeeded", ""

    try:
        async with asyncio.timeout(timeout or None):
            return await attempt()
    except TimeoutError:
        return "timed_out", "The run hit its deadline."
    except OutputLimit:
        return "failed", "The agent produced more output than the configured limit."
    except OSError:
        return "failed", "The agent could not start. Check its executable and folder."
    finally:
        await _finish(process, tasks, grace)


async def _finish(process, tasks, grace):
    """Reap children even if the caller cancels us repeatedly."""

    async def cleanup():
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process is not None:
            await terminate(process, grace)

    job = asyncio.create_task(cleanup())
    interrupted = False
    while not job.done():
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError:
            interrupted = True
    job.result()
    if interrupted:
        raise asyncio.CancelledError
