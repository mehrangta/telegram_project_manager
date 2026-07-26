# Codex plan for mehrangta/telegram_project_manager#43

Job: `c-e9e1c1ff` · Revision: 1

Issue #43 is already functionally satisfied by the admin-only `/queue` command merged on July 17, 2026 in commit `ed619ee`. It lists running and queued `/code`, `/ask`, `/brainstorm`, and `/do` Codex work for the current chat or exact forum topic. The recommended disposition is to close the issue as already implemented, with no code or schema changes. If the requested word “terminals” implies a new command name or different scope/status semantics, the decisions below must be resolved before implementation.

## Steps

1. **Confirm Existing Coverage** — Treat `/queue` as the current implementation baseline. It is registered in the Telegram router, requires an admin, rejects arguments, queries each Codex-backed service using the incoming `chat_id` and exact `thread_id`, groups results into running and queued sections, safely escapes dynamic text through `outgoing_message`, and returns a scope-specific empty message when no work exists. Record this evidence on issue #43 and close it without a patch if the existing interface satisfies the request.
   - Likely files: `src/telegram_project_manager/bots/codex_queue/commands.py`, `src/telegram_project_manager/main.py`, `src/telegram_project_manager/bots/commit_manager/commands.py`, `README.md`, `tests/test_codex_queue.py`, `tests/test_commands.py`
2. **Define Command Compatibility** — If a new command name is required, add `/terminals` as a backward-compatible alias in `CodexQueueManager.handle` rather than replacing `/queue`. Normalize bot-addressed forms such as `/terminals@ProjectBot` through the same command comparison, require no arguments, and route both names through one shared snapshot/rendering path so their output and failure behavior cannot diverge. Keep `/queue` operational to avoid breaking documented usage, existing messages, and user habits. Return alias-specific usage text only when the invoked alias has unexpected arguments.
   - Likely files: `src/telegram_project_manager/bots/codex_queue/commands.py`
3. **Reuse Active Work Sources** — Do not introduce a terminal table, process scan, or new persistence layer. Continue obtaining code jobs from SQLite via `CodeJobService.queue_snapshot`, repository questions from `AskService` in-memory queue metadata, brainstorm jobs from `BrainstormService` in-memory queue metadata, and full-access jobs from durable `DoService` records. Preserve deterministic oldest-first ordering and exact chat/topic filtering. Under the recommended semantics, classify only actual Codex execution phases as running and admitted work as queued; continue excluding code jobs paused for clarification, approval, CI, retry, merge, deployment, failure, interruption, or completion.
   - Likely files: `src/telegram_project_manager/bots/code_manager/service.py`, `src/telegram_project_manager/bots/ask_manager/service.py`, `src/telegram_project_manager/bots/ideas/service.py`, `src/telegram_project_manager/bots/do_manager/service.py`, `src/telegram_project_manager/platform/storage/db.py`
4. **Preserve Failure Behavior** — Keep the existing admin authorization boundary and do not expose global job information across chats or topics. Return `Unauthorized. Admin role required.` for direct manager calls by non-admins, a clear usage response for arguments, and `No Codex work is running or queued for this chat/topic.` for an empty scope. Preserve request/question preview redaction, safe handling of missing or symlinked `/do` payloads, HTML escaping, and image-count labels. Document that `/ask` and `/brainstorm` entries disappear on bot restart, while code and `/do` state is database-backed; do not claim the command enumerates operating-system terminals or Codex SDK threads independently of managed jobs.
   - Likely files: `src/telegram_project_manager/bots/codex_queue/commands.py`, `src/telegram_project_manager/bots/ask_manager/service.py`, `src/telegram_project_manager/bots/do_manager/service.py`, `src/telegram_project_manager/platform/responses.py`
5. **Update User Documentation** — Only if an alias is selected, add `/terminals` beside `/queue` in Telegram `/help` and README command references, describe it as an alias for scoped running and queued Codex work, and retain `/queue` as the canonical or explicitly equivalent command. No database migration, configuration change, worker change, or deployment sequencing is required; rollout consists of deploying and restarting the bot process normally.
   - Likely files: `src/telegram_project_manager/bots/commit_manager/commands.py`, `README.md`
6. **Extend Regression Coverage** — If code changes are requested, parameterize or duplicate command-handler tests to cover `/queue`, `/queue@Bot`, `/terminals`, and `/terminals@Bot`; verify identical mixed-service rendering, exact chat/topic scoping, admin enforcement, argument rejection, empty responses, dynamic-text escaping, redaction, ordering, and inclusion of every declared running/queued code status. Add a help assertion for the alias while preserving the existing `/queue` assertion. No new database-schema tests are needed because the implementation reuses existing snapshots.
   - Likely files: `tests/test_codex_queue.py`, `tests/test_commands.py`, `tests/test_ask_manager.py`, `tests/test_brainstorm.py`, `tests/test_do_manager.py`

## Validation

- Run the focused queue tests: `uv run python -m unittest discover -s tests -p 'test_codex_queue.py'`.
- Run help and command regression tests: `uv run python -m unittest discover -s tests -p 'test_commands.py'`.
- Run queue-source lifecycle tests: `uv run python -m unittest discover -s tests -p 'test_ask_manager.py'`, `uv run python -m unittest discover -s tests -p 'test_brainstorm.py'`, and `uv run python -m unittest discover -s tests -p 'test_do_manager.py'`.
- Run the complete repository suite before rollout: `uv run python -m unittest discover -s tests`.
- Manually verify in a private chat and a forum topic that the selected command shows only the exact scope, renders running and queued sections, and returns the correct empty-scope message.

## Risks

- Implementing another command without clarifying intent may duplicate `/queue` without adding user value or may create two names whose behavior later diverges.
- Replacing `/queue` instead of adding an alias would be an unnecessary compatibility break because `/queue` is already documented and tested.
- A global listing would expose repository names, prompts, and operational activity across otherwise isolated chats or forum topics; the existing exact-scope behavior should remain the default.
- The phrase “active terminals” may be interpreted as only currently executing SDK turns, while existing `/queue` intentionally includes admitted queued work and excludes paused nonterminal code jobs.
- `/ask` and `/brainstorm` queue metadata is process-local, so those active entries vanish after a bot restart; presenting the result as an authoritative operating-system terminal inventory would be inaccurate.
- Changing status membership independently from queue admission constants could cause displayed counts to disagree with concurrency and queue-limit enforcement.

## Open questions

1. Given that `/queue` already provides this capability, what should issue #43 deliver?
   A. Close as already implemented by `/queue`; make no code changes. (recommended)
   B. Add `/terminals` as a backward-compatible alias with identical behavior.
   C. Replace `/queue` with `/terminals` and migrate documentation and tests.
2. If a new command is required, what visibility scope should it use?
   A. Current chat or exact forum topic, matching `/queue`. (recommended)
   B. All active jobs across every chat and topic.
   C. Only jobs submitted by the requesting admin.
3. Which managed jobs should count as active?
   A. Running and queued jobs, matching `/queue`. (recommended)
   B. Only jobs currently executing a Codex turn.
   C. Every non-completed job, including paused, failed, and interrupted work.
