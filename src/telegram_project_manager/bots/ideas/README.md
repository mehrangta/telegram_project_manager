# Ideas Bot

Provides the read-only `/brainstorm` command for generating repository-grounded
ideas for new capabilities or meaningful extensions, not bug finding or maintenance
reviews, plus an optional per-repository UTC interval scheduler. Configuration is
managed with `/repo brainstorm ...`; manual runs detect the repository from the exact
configured chat or topic destination, with the active repository used for
fallback. Manual runs update their queued Telegram reply with the terminal result;
after a successful manual run, a separate completion notification is posted and
removed after five seconds. Scheduled runs post the terminal result directly
without the additional notification. Execution uses detached worktrees and the
Codex plan model. Idea fields are generated to a bounded single-message format and
delivered as complete text rather than shortened after generation.
