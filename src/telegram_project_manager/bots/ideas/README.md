# Ideas Bot

Provides the read-only `/brainstorm` repository analysis command and its optional
per-repository UTC interval scheduler. Configuration is managed with
`/repo brainstorm ...`; manual runs detect the repository from the exact configured
chat or topic destination, with the active repository used for disambiguation and
fallback. Execution uses detached worktrees and the Codex plan model.
