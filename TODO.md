# TODO

The bot's purpose is to start and continue long-running Codex sessions from
Discord, keeping users informed while work is in progress.

Implementation plan: [Remote long-running tasks](docs/superpowers/plans/2026-09-08-remote-long-running-tasks.md).

- [ ] Add persistent sessions. Currently, every call starts a new Codex invocation
  without carrying conversation context forward. Define how Discord conversations
  map to sessions and how users start, resume, and reset them.
- [ ] Show which model or models failed. Preserve available model metadata and
  report failures per model instead of showing everything as `unavailable`.
- [ ] Design support for long-running tasks, including agents that run for
  5-10 minutes or longer. Decide how active tasks are tracked and how completion,
  failure, and cancellation are communicated.
- [ ] Clarify timeout semantics. Investigate how to distinguish a failure to reach
  the server from an agent that connected and is still working. Separate connection
  or startup timeouts, inactivity timeouts, and overall execution limits where
  supported, and define what happens to the running agent when each limit expires.
- [ ] Acknowledge requests promptly and provide progress updates or streamed
  output in Discord. Let users distinguish bot receipt, confirmed agent activity,
  and completion or failure, so they can tell whether a call went through even
  during a long run. Decide what to show when no new output arrives and how to
  handle Discord message limits and rate limits.
