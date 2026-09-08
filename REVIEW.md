# Extraction Review

Reviewed September 7, 2026. Scope: the Sad Hamster Discord bot
and this standalone replacement. An independent read-only review covered the
original and extracted code.

## Findings Addressed

| Finding in the original | Change |
| --- | --- |
| Timeout was parsed after starting Codex; invalid input could abandon the process | Validate settings once, before connecting or spawning |
| Cancellation left Codex running; timeout killed only its direct process | Isolate each run in a POSIX process group, kill child tools, drain pipes and reap on exit |
| A prompt beginning with `--` could be interpreted as a CLI flag | Send prompts through stdin |
| Output buffering had no limit | Cap each captured stream at 1 MiB |
| Raw failed-run diagnostics could contain echoed private prompts | Log request IDs, exit status, and exception type rather than transcripts |
| `.env` was not loaded | Use python-dotenv, environment precedence, and file-relative paths |
| Direct `Client.start()` bypassed managed client shutdown | Use discord.py's `Client.run()` |
| Delivery errors and unexpected runner failures had little recovery coverage | Handle delivery failures, release the lock, and provide clean failure replies |
| Bot code, dependencies, tests, and setup | Own package, console entry point, configuration template, README, and CI |

The extraction also removed repetitive ignored-message logging, the command
string parsing and bare-command repair path, the file-name collision loop,
and one-use mention/chunk helper functions. The runtime remains one module.
Validation and process cleanup add necessary code; this is not a claim that
the total source line count is smaller than the original.

The follow-up review found a bare timeout key in `.env` could still produce
a traceback. It now produces a named configuration error, with regression
coverage for missing, empty, nonnumeric, infinite, and nonpositive values.

## Verification and Limits

Tests cover settings, precedence, logs, metadata, safe prompt transport,
model/effort arguments, access restrictions, busy handling, notification
limits, Discord delivery failures, and recovery. Real subprocess tests cover
empty output, failure, output overflow, timeout, and cancellation of ordinary
child tools. Packaging is checked through a standalone install and build.

The CLI's text metadata header/footer is not a stable structured API. Changed
or missing fields display `unavailable`; the final answer comes separately
from stdout. A future JSON event adapter could improve metadata handling but
needs a defined source for the effective model and reasoning effort.

This is deliberately a private, single-user, single-channel bot. There is no
queue, saved conversation state, task retry, or automatic log retention. It
supports macOS/Linux process groups; deliberately detached tools can escape
group cleanup. Discord connectivity and real Codex authentication require a
live check by the operator. No live messages are sent by the test suite.
