# Remote Long-Running Tasks Implementation Plan

> **For agentic workers:** Use the executing-plans skill to implement this plan task-by-task. Steps use checkbox syntax for tracking. This document proposes implementation; it does not record completed features.

**Goal:** Reliably trigger long-running Codex tasks remotely through Discord, continue their sessions, observe progress, and recover understandable results when execution or delivery fails.

**Architecture:** Keep Python, discord.py, and the local Codex CLI. Add a streaming CLI adapter, a SQLite job/session store, and a job manager independent of Discord message delivery. Use explicit Codex session IDs for conversation continuity and separate job state from connection observations and delivery state.

**Tech Stack:** Python 3.12+, asyncio, sqlite3, discord.py, python-dotenv, unittest; Codex JSONL output and explicit session resumption, subject to the compatibility gate below.

---

## Evidence and Scope

Reviewed `TODO.md`, `README.md`, `.env.example`, `pyproject.toml`, `src/models.py`, `src/sad_hamster_bot.py`, and `tests/test_bot.py` on 2026-09-08.

- `run_codex` buffers both streams until exit and wraps the entire invocation in a 30-second timeout. Timeout currently means the local execution deadline expired; it does not establish whether the server was reached. Its cleanup kills the process group.
- `run_with_fallback` retries only the literal message `Codex timed out.`. Five configured models and three retries per model permit 20 invocations, roughly ten minutes at the default timeout, repeatedly terminating work after 30 seconds. Other failures return immediately. Earlier attempts and total job duration are lost.
- Timeout and startup error returns use default `unavailable` metadata even though the attempted model and effort were supplied by the bot.
- `on_message` holds a lock while awaiting the whole run and exposes only typing feedback. There is no durable job ID, session mapping, status command, cancellation command, or result recovery.
- README configuration and retry claims are stale: code reads `CODEX_MODELS` and `CODEX_RETRIES`, while the README describes single-model overrides and no automatic retries.
- Installed `codex-cli 0.153.4` help confirms `exec --json`, `exec resume --json SESSION_ID -`, and stdin prompts. A new OS process can resume an existing conversation; session continuity does not require keeping one CLI process alive between turns. Help does not establish event schemas or prove server acceptance.
- Official documentation retrieval was unavailable during planning: sandbox DNS failed, and both external-fetch approval reviews exceeded their deadline. The official URLs below are verification targets, not fetched supporting evidence. Task 1 makes event schema and server-activity verification an implementation gate.
- Baseline `.venv/bin/python -B -m unittest discover -s tests -v`: 19 tests, four failures and one error. Failures concern retry metadata formatting, missing author mentions, legacy reasoning validation, and an unmentioned message reaching the runner. Reconcile these expectations explicitly in Task 1.

The remote host must stay awake with the bot service running. Discord disconnects must not stop jobs. In this version, a bot process crash or host restart interrupts active execution; persist evidence and require explicit continuation instead of promising transparent crash survival. An independently supervised worker that survives bot restarts is a separate future capability, not a prerequisite for 5-10 minute tasks.

## Proposed User Workflow

1. In the configured private channel, mention the bot with a task. Commit the job record and send `Accepted job <id>; starting Codex` before launching it. If initial acknowledgment cannot be delivered, do not launch hidden work; mark the job as not started.
2. Edit that status message as events arrive. Show job ID, selected model, elapsed time, last observed activity, and whether server acceptance is confirmed or unknown. Keep a heartbeat visible during silence.
3. Mention the bot with `status [job-id]`, `cancel [job-id]`, or `result <job-id>` while it is busy. These commands never enter the model prompt or wait for the execution lock.
4. On completion, edit status and deliver the final response, mentioning the owner once. On failure, show each attempted model, its outcome and duration, and the last known task state.
5. A later prompt resumes the current session. `session new` starts a new conversation on the next prompt; `session list` lists bot-owned sessions; `session resume <id>` selects an existing bot-owned session. Reject session changes while a job is active.

Default scope: one authorized user, one configured channel, one active job per workspace, no automatic queue. All prompts and control commands require a bot mention; this follows the existing tests' intended addressing contract. Use channel-level sessions initially, with the key `(user_id, channel_id, canonical_workdir)`. Discord thread routing and parallel workspace jobs are not needed for these TODOs.

## State and Timeout Contract

Keep job outcome, activity observations, and delivery status separate:

```text
accepted -> starting -> running -> succeeded
                       |       -> failed
                       |       -> cancelling -> cancelled
                       |       -> timed_out (explicit overall deadline)
accepted/starting      -> failed (definite launch or protocol failure)
any nonterminal state  -> interrupted (owner process lost)

Activity: CLI started / local event observed / server response observed / quiet
Delivery: pending / delivered / retryable failure / blocked
```

`running` means the local process is alive, not that a server accepted the request. A thread or turn lifecycle event must not be labeled server acceptance unless the documented protocol establishes that meaning. A provider response or model-generated item can establish activity; absence of one cannot establish that no work occurred. Track CLI event time separately from model/tool activity so local retry chatter cannot hide a stall.

| Setting | Proposed default | Behavior |
| --- | --- | --- |
| `CODEX_STARTUP_WARN_SECONDS` | 30 | Warn that useful agent activity has not yet been observed; continue waiting. |
| `CODEX_IDLE_WARN_SECONDS` | 120 | Mark the job quiet after no meaningful activity; keep the process running. |
| `CODEX_MAX_RUN_SECONDS` | 0 (disabled) | Optional overall job deadline covering all attempts and backoff; cancellation stops the owned process group. |
| `DISCORD_PROGRESS_SECONDS` | 15 | Coalesce event changes into at most one periodic status edit per interval. |
| `DISCORD_HEARTBEAT_SECONDS` | 30 | Update elapsed time and last activity even when no events arrive. |
| `BOT_STATE_DIR` | `state` | Private SQLite database and retained final results; resolve relative to the env file. |

Keep `CODEX_TIMEOUT_SECONDS` as a deprecated alias for the overall deadline when explicitly set; warn at startup that an existing value of 30 still stops work after 30 seconds. Reject conflicting old/new settings. Update the example env to the new unlimited default. Accept only finite positive warning/update intervals and a finite nonnegative maximum duration.

Do not add a bot-side hard connection timeout unless the CLI exposes reliable connection-stage evidence. Label known provider connection errors as such; label ambiguous local deadlines `overall deadline exceeded; last observed state ...`. An agent running for ten minutes is normal under the default configuration. Cancellation and explicit deadlines stop work and never trigger automatic replay.

## Retry and Failure Contract

Create an attempt record before starting each process. Preserve requested model and effort even if launch fails; store reported model separately when the protocol supplies it. Unknown tokens remain unknown, not zero. Include every attempt on both eventual success and complete failure, plus whole-job elapsed time and per-attempt duration.

Use typed outcomes rather than matching display text. Classify spawn/configuration/authentication failures, explicit provider rejection, transport failure with uncertain acceptance, agent failure after activity, malformed output, cancellation, deadline expiry, and interrupted execution. Summarize errors through an allowlist of safe categories; do not forward raw stderr, reasoning, environment values, or command output.

Automatic fallback is allowed only when structured evidence establishes rejection before execution, such as an explicitly reported unsupported model, and the installed protocol supports that conclusion. Retriable rate limits require equally clear rejection evidence. Use bounded backoff (1, 2, 4 seconds, honoring a larger provider retry delay within the remaining job deadline), configured retry counts, and the ordered `CODEX_MODELS` list. When evidence is insufficient, stop and show an explicit continuation option. Never fall back merely because output is quiet, a timeout expires, or a process exits nonzero. Default to zero automatic retries until the compatibility gate validates a classification.

## File Map

| File | Responsibility |
| --- | --- |
| `src/models.py` | Typed jobs, sessions, attempts, execution outcomes and progress snapshots; final summary formatting. |
| `src/codex_runner.py` (new) | CLI argv, incremental JSONL parsing, process lifecycle, bounded stream handling, normalized execution events. |
| `src/job_store.py` (new) | SQLite schema, transactions, session ownership, job/attempt records and delivery checkpoints. |
| `src/job_manager.py` (new) | Single-job admission, session selection, deadlines, retry policy, cancellation and reconciliation. |
| `src/sad_hamster_bot.py` | Settings, startup/logging and Discord authorization, commands and progress delivery. |
| `tests/test_bot.py` | Existing settings and Discord contracts, updated for the new runner boundary. |
| `tests/test_codex_runner.py` (new) | JSONL fixtures and real fake-process execution tests. |
| `tests/test_job_store.py` (new) | Persistence, ownership, deduplication and restart behavior. |
| `tests/test_job_manager.py` (new) | Job lifecycle, sessions, retries, fake-clock deadlines and cancellation. |
| `tests/fixtures/codex/` (new) | Sanitized version-labeled event examples with no user data. |
| `pyproject.toml`, `MANIFEST.in` | Register new flat modules and ship relevant operating documentation. |
| `README.md`, `.env.example`, `.gitignore`, `TODO.md` | Actual settings, service operation, state exclusions and completion tracking. |

## Implementation Tasks

Implement in this order. Each task begins with a focused failing test, adds the behavior, reruns its focused tests, and ends with a reviewed commit scoped to that task. Keep existing unrelated work intact. This is an implementation design, not a full source-code replacement.

### Task 1: Verify the CLI Contract and Reconcile Baseline Tests

- [ ] Retrieve the official non-interactive documentation at `https://developers.openai.com/codex/noninteractive/` and CLI reference at `https://developers.openai.com/codex/cli/reference/`. Record documented event fields, terminal success/failure semantics, resume flag placement, model metadata availability, and what can prove server activity. Local help is evidence for flags only.
- [ ] Run `codex --version`, `codex exec --help`, and `codex exec resume --help`; record a tested CLI version. Use a disposable workspace for a short first turn and an explicit-ID resumed turn. Confirm JSONL event ordering and context recall without assuming that `thread.started` means a server connection. Sanitize fixtures into `tests/fixtures/codex/`.
- [ ] If JSONL lacks required metadata, retain requested model/effort and label reported values unknown. If it lacks server-stage evidence, retain an unknown acceptance state and disable ambiguous automatic retries. If it lacks token deltas, use item-level updates and heartbeats; do not promise token streaming.
- [ ] Update `tests/test_bot.py` to retain retries in summaries, restore one owner mention in final delivery, require a mention before prompt/control processing, and validate supported legacy model/effort settings. Add tests defining precedence: reject simultaneous explicit `CODEX_MODELS` and legacy model/effort overrides; otherwise legacy settings produce a single configured attempt. Preserve current ordered model defaults unless deliberately configured otherwise.
- [ ] Run `.venv/bin/python -B -m unittest discover -s tests -v`. Expected: baseline green after targeted corrections; do not delete failing assertions merely to make the suite pass. Add a fixture compatibility test that fails cleanly for unsupported required schema changes.

### Task 2: Add Typed Outcomes and Complete Attempt Reporting

- [ ] In `tests/test_job_manager.py`, specify that two failed attempts followed by success retain all three requested model/effort pairs, outcomes and durations; an immediate spawn failure still identifies the requested model; timeout never enables retry by its message text.
- [ ] Extend `src/models.py` with `JobState`, `AttemptOutcome`, `SessionRecord`, `JobRecord`, `AttemptRecord`, and `ProgressSnapshot`. Give jobs stable UUIDs, attempts sequential numbers, and nullable reported metadata. Use monotonic time for live elapsed calculations and UTC timestamps for persisted history.
- [ ] Replace `run_with_fallback` string comparisons with a typed retry eligibility decision. Retain per-attempt evidence, sum known token usage without implying unknown usage is zero, and measure elapsed time from job admission to terminal outcome, including backoff.
- [ ] Format attempt history as compact lines, for example `model-a / low: provider rejected (2s); model-b / medium: succeeded (8m 12s)`. Keep unknown measured fields explicit. Test both all-failed and eventual-success summaries and long histories split within Discord limits.
- [ ] Run `.venv/bin/python -B -m unittest discover -s tests -p 'test_job_manager.py' -v`; expected: model history and retry classification tests pass.

### Task 3: Stream Codex Execution Without Killing Quiet Work

- [ ] In `tests/test_codex_runner.py`, add a fake executable emitting fragmented JSONL, multiple events per read, multibyte text, delayed output, malformed events, stderr floods and EOF without a terminal event. Assert progress arrives before process completion and a quiet live process stays alive.
- [ ] Move subprocess ownership from `run_codex` into `src/codex_runner.py`. Launch `codex exec --json --skip-git-repo-check --model <model> -c 'model_reasoning_effort="<effort>"' -` with argv and stdin, not a shell. For follow-ups use the verified `exec resume` flags and an explicit ID. Preserve cwd, sandbox/approval settings and Discord environment filtering; never use `--last` or `--ephemeral`.
- [ ] Parse stdout incrementally with the standard JSON parser. Normalize only verified event types into session IDs, agent messages, tool lifecycle summaries, usage and terminal outcomes. Ignore unknown optional event types; fail with a typed compatibility error when required structure is unusable. A process exit alone is not verified task success.
- [ ] Drain stderr concurrently into a bounded diagnostic tail. Bound individual events (initially 1 MiB), retained assistant text (initially 1 MiB) and progress memory; discard already-consumed tool output instead of accumulating a 1 MiB lifetime stream cap. If a required event exceeds the limit, report an output-limit failure, stop cleanly and do not retry.
- [ ] Implement startup/idle warnings independently of the optional overall job deadline. Use a monotonic clock. On explicit cancellation or deadline, send SIGTERM to the owned process group, allow five seconds, then SIGKILL and reap; do not release the workspace slot until cleanup completes. Retain current protection against child tools continuing after cancellation.
- [ ] Run `.venv/bin/python -B -m unittest discover -s tests -p 'test_codex_runner.py' -v`; expected: bounded-memory, early-progress, terminal-state and process cleanup assertions pass. Simulate ten minutes through an injected clock without making routine tests sleep ten minutes.

### Task 4: Persist Sessions, Jobs and Results

- [ ] In `tests/test_job_store.py`, cover reopen-after-close persistence, session ownership, canonical workspace separation, duplicate Discord message IDs, transactional admission, missing session IDs and incomplete delivery.
- [ ] Implement `src/job_store.py` using sqlite3 with schema versioning and transactions. Store sessions (owner/channel/workspace, Codex ID, created/last-used timestamps), jobs (source message ID unique, session, state, timing, status message ID, result), attempts (job/number/model/effort/outcome/evidence), and delivery chunks (job/index/message ID/state). Add a partial unique constraint preventing two active jobs for one workspace.
- [ ] Store the exact session ID as soon as its verified event arrives, including when a later turn fails. Resume only that selected bot-owned ID. A missing or expired Codex session yields an explicit error and `session new` option, never a silent fresh session.
- [ ] Keep state in `BOT_STATE_DIR` with private directory/file permissions; ignore `state/` in Git. Retain final results for seven days by default and document that they may contain private content; prune terminal job results on startup and daily, preserving active jobs and session mappings. Keep prompts out of general logs and do not persist a replayable prompt queue.
- [ ] On startup, reconcile unfinished jobs as interrupted unless a verified owned worker is still active. Never kill a PID solely because it appears in SQLite, and never auto-replay an interrupted prompt. Block new workspace execution until uncertain orphan ownership is resolved; provide a clear status explaining the conflict.
- [ ] Run `.venv/bin/python -B -m unittest discover -s tests -p 'test_job_store.py' -v`; expected: reopening preserves context mapping/results, duplicate admission is prevented, and interrupted jobs are visible.

### Task 5: Manage Jobs and Expose Remote Controls

- [ ] In `tests/test_job_manager.py`, cover admission races, command processing during execution, continuation across two prompts, explicit new/resume selection, cancellation during backoff and process startup, and a deadline shared across all attempts.
- [ ] Implement `src/job_manager.py` with explicit owned asyncio tasks, one workspace execution slot and transactional job admission. Capture the selected session at admission. Deduplicate source message IDs before spawning; reject a second task with the active job ID and status while allowing controls.
- [ ] In `CodexBot.on_message`, parse mentioned `status`, `cancel`, `result`, and `session` commands before busy handling. Apply the existing user/channel authorization to all commands and enforce ownership for every supplied ID. Return usage for malformed commands; never pass them to Codex.
- [ ] Commit accepted job state, deliver acknowledgment, then start the manager task and return from the event callback. Persist acknowledgment failures as not-started failures. Make cancellation idempotent; a cancel racing completion must not replace verified success with cancelled state.
- [ ] Ensure Discord reconnects do not recreate jobs or execution tasks. On graceful shutdown, stop and reap owned execution, persist interruption, and flush delivery state. Document that resuming a session continues conversation context, not an arbitrary stopped subprocess.
- [ ] Run `.venv/bin/python -B -m unittest discover -s tests -p 'test_job_manager.py' -v` and `.venv/bin/python -B -m unittest discover -s tests -p 'test_bot.py' -v`; expected: status/cancel stay responsive and a follow-up uses the saved ID.

### Task 6: Deliver Progress and Recover Final Responses

- [ ] Extend `tests/test_bot.py` with acknowledgment-before-spawn ordering, heartbeat during silence, coalesced edits under event bursts, active status/cancel commands, reconnect recovery, HTTP 429/403/404 cases, and completion delivery after transient disconnect.
- [ ] Add a delivery loop to `src/sad_hamster_bot.py` that reads persisted job state and a bounded latest progress snapshot independently of the runner. Show elapsed time, requested model, current attempt and last meaningful activity. Use labels such as `CLI started; server response not yet observed` and `Still running; last activity 2m ago` when supported by evidence.
- [ ] Edit one status message at the configured interval; show only brief assistant commentary or generic tool lifecycle labels. Keep raw tool output and private reasoning out of Discord. Use discord.py rate-limit handling and bounded retry delays without blocking stdout reads; 403 marks delivery blocked, 404 permits replacing a deleted status message.
- [ ] Persist terminal output before sending final chunks. Record successfully delivered chunk IDs, retry known transient failures and provide `result <job-id>` for recovery. For uncertain sends, reconcile recent bot messages using job/chunk identifiers when permissions allow; otherwise report uncertain delivery rather than claiming exactly-once behavior. Add Read Message History to documented permissions for reconciliation.
- [ ] Preserve 1,900-character chunk limits and balanced code fences; disable mass mentions, and mention the owner only in the first final chunk. A final-delivery failure must never turn a successful execution into failure or rerun the task.
- [ ] Run `.venv/bin/python -B -m unittest discover -s tests -p 'test_bot.py' -v`; expected: bounded progress traffic, recoverable output and execution unaffected by delivery failures.

### Task 7: Integrate, Document and Exercise Long Runs

- [ ] Update `Settings.load`, `.env.example`, and README with the timeout table, ordered model configuration, retry evidence policy, state retention, commands and required Discord permissions. Remove obsolete text-only output, fresh-session-only and no-retry claims. Document authenticated CLI setup, wrapper JSONL compatibility and known unsupported event behavior.
- [ ] Register `codex_runner`, `job_store`, and `job_manager` in `pyproject.toml`. Update `MANIFEST.in` for shipped documentation. Add macOS launchd and Linux systemd user-service examples to README with restart-on-failure, explicit working directory/env-file paths and host sleep limitations; explain interrupted-job behavior on service restart.
- [ ] Run `.venv/bin/python -B -m unittest discover -s tests -v`, `.venv/bin/ruff check .`, and `.venv/bin/python -m build`. Expected: all tests/lint pass and the built distribution includes/imports all new modules. Record and resolve baseline lint issues separately from unrelated user edits.
- [ ] Perform a ten-minute fake-run soak: delayed activity, multiple tool events and a five-minute silent interval. Verify prompt acknowledgment, continuing heartbeats, status responsiveness, bounded memory, no fallback, and eventual final delivery. Separately cancel a child-spawning run and confirm no owned child remains.
- [ ] In the configured private Discord environment, run one short live task, a follow-up requiring previous context, and one 5-10 minute task. Temporarily disconnect Discord while the agent runs, reconnect, and retrieve the result. Exercise explicit deadline expiry and verify that no new model attempt starts afterward. Record CLI version, job IDs, durations and outcomes without copying private transcripts into the repository.
- [ ] Mark TODO checkboxes complete only after their acceptance evidence passes; link operating instructions and retain any unimplemented limitation explicitly.

## Acceptance Coverage

| TODO requirement | Tasks | Evidence required |
| --- | --- | --- |
| Persistent sessions | 1, 4, 5 | Second turn uses saved explicit ID and recalls first-turn context; restart preserves mapping; new/resume controls work. |
| Identify failed models | 2, 3, 6 | All-failed and eventual-success cases show every attempted model, effort, outcome and duration, including launch failures. |
| Long-running tasks | 3, 5, 7 | Ten-minute soak and live run finish without a default deadline; status/cancel remain usable; service operation documented. |
| Understand timeouts | 1, 2, 3, 7 | Quiet run survives; definite provider failure differs from unknown acceptance; explicit overall deadline cancels once with no replay. |
| Acknowledgment and streaming | 3, 6, 7 | Acknowledgment precedes execution; item progress/heartbeats arrive before completion; disconnect does not stop execution; result is recoverable. |

## Review Gates

The first release should include all seven tasks: merely extending the 30-second timeout does not satisfy remote task operation. The key compatibility uncertainty is how much server-stage evidence the installed CLI provides. Resolve it with documented fixtures and conservative labels, not guesses based on elapsed time. Rich approval interaction or true token deltas may justify a later Codex app-server adapter; keep them out of the required CLI scope unless the compatibility gate proves the CLI cannot support the workflow above.
