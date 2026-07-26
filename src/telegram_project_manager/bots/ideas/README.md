# Ideas Bot

Provides the read-only `/brainstorm` command for generating repository-grounded
ideas for new capabilities or meaningful extensions, not bug finding or maintenance
reviews, plus an optional per-repository UTC interval scheduler. Configuration is
managed with `/repo brainstorm ...`; manual runs use the exact chat or topic's active
repository, while scheduled runs use the destination saved for their repository.
Execution uses detached worktrees and the Codex plan model.
