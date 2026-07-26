# Codex plan for mehrangta/telegram_project_manager#33

Job: `c-a3fc162e` · Revision: 1

Change manual `/brainstorm` execution from two Telegram messages to one lifecycle message: send the existing queued acknowledgement as a reply to the command, capture its Telegram message ID, and edit that message with the final result, failure, or cancellation. Preserve scheduled brainstorm behavior, existing command resolution, queue semantics, database status tracking, response rendering, and Telegram-length safeguards. No database migration or public command syntax change is required.

## Steps

1. **Capture the acknowledgement message ID** — Change the brainstorm send helper to return the integer `message_id` from `TelegramBotApi.send_message`. In `BrainstormService.submit`, retain the original command `message_id` only as `reply_to_message_id` for the queued acknowledgement, capture the acknowledgement's bot message ID, and pass that ID through `_start_task` into `_run`. Use explicit names such as `reply_to_message_id` and `status_message_id` to prevent mixing the user's command ID with the bot-owned editable message ID. Keep the existing behavior that a failed acknowledgement marks the claimed run failed and prevents task creation.
   - Likely files: `src/telegram_project_manager/bots/ideas/service.py`
2. **Add a unified terminal delivery path** — Introduce a focused helper that renders text with `outgoing_message` and either edits an existing `status_message_id` or sends a new message when no editable message exists. For edits, call the existing `TelegramBotApi.edit_message_text` with the same HTML parse mode, link-preview setting, and `reply_markup(include_empty=True)` convention used by progress reporters. Treat Telegram's `message is not modified` response as an idempotent success; propagate other Telegram API errors so the existing audit and database failure paths remain authoritative.
   - Likely files: `src/telegram_project_manager/bots/ideas/service.py`, `src/telegram_project_manager/platform/telegram_bot.py`
3. **Update manual completion and failure flow** — Use the unified delivery helper for `_run` success and `_send_failure`. Manual runs must edit the queued acknowledgement into `_render_result(...)` on success and into `Repository brainstorm failed` on handled Codex, workspace, repository, validation, or unexpected errors. On service-shutdown cancellation, best-effort edit the same message to a cancelled/interrupted terminal state before re-raising `CancelledError`, so an accepted manual run does not remain visibly queued. If editing itself fails because the message was deleted or is no longer editable, log and audit the Telegram error and mark the run failed without sending a second fallback message, preserving the single-message contract.
   - Likely files: `src/telegram_project_manager/bots/ideas/service.py`
4. **Preserve scheduled and queue compatibility** — Pass `status_message_id=None` for scheduled runs. The unified delivery helper must therefore continue sending exactly one new scheduled result or failure to the configured chat/topic, without adding a queued acknowledgement. Do not change `BrainstormQueueEntry`, `/queue` rendering, concurrency limits, run claiming, scheduling cadence, repository selection, Codex inputs, result schema, or database schema. Process-restart recovery remains unchanged because brainstorm tasks and queue entries are already in-memory only.
   - Likely files: `src/telegram_project_manager/bots/ideas/service.py`, `src/telegram_project_manager/bots/ideas/commands.py`, `src/telegram_project_manager/bots/codex_queue/commands.py`, `src/telegram_project_manager/platform/storage/db.py`
5. **Expand brainstorm service tests** — Extend `FakeBot` with an `edited` collection and `edit_message_text` implementation. Update the manual success test to assert one `send_message` call, one edit targeting the acknowledgement's returned message ID, the acknowledgement replying to the original command, and the final rendered improvements appearing only in the edit. Keep the scheduled test asserting one send and zero edits. Add a failing Codex or workspace case asserting that the same acknowledgement is edited into the failure response, the database records `last_status=failed`, and no second message is sent. Add an edit-error case asserting no fallback send and failed run/audit behavior; add cancellation coverage if cancellation messaging is implemented in `_run`.
   - Likely files: `tests/test_brainstorm.py`
6. **Document the one-message lifecycle** — Update the brainstorm documentation to state that a manual `/brainstorm` run posts a queued reply and updates that same Telegram message when the run completes or fails, while scheduled runs continue posting their result directly. Keep command syntax and configuration documentation unchanged.
   - Likely files: `README.md`, `src/telegram_project_manager/bots/ideas/README.md`

## Validation

- Baseline verified with `TPM_TEST_CODEX_STUBS=1 PYTHONPATH=src python3 -m unittest tests.test_brainstorm` (22 tests passed).
- After implementation, run the focused suite with `TPM_TEST_CODEX_STUBS=1 PYTHONPATH=src python3 -m unittest tests.test_brainstorm`.
- Run the complete repository suite with `TPM_TEST_CODEX_STUBS=1 PYTHONPATH=src python3 -m unittest discover -s tests`.
- Where `uv` is installed, the equivalent project-managed focused command is `TPM_TEST_CODEX_STUBS=1 uv run python -m unittest tests.test_brainstorm`; `uv` was not available in the planning environment.

## Risks

- Telegram cannot edit a message that was deleted, is too old under applicable platform constraints, or is no longer editable by the bot. To preserve the issue's one-message requirement, the implementation should audit and log this condition rather than sending a second result message, which may leave the queued text visible.
- The bot message ID must remain distinct from the original `/brainstorm` command message ID. Confusing them would attempt to edit a user-owned message and fail; explicit parameter names and assertions should guard this boundary.
- Scheduled runs do not create an acknowledgement message and therefore must retain the send-only path. Applying edit-only behavior globally would silently lose scheduled results.
- Active brainstorm tasks are not recovered after process restart, so an acknowledgement sent immediately before a restart can remain queued. Persisting and recovering message IDs would require a broader schema and job-recovery design outside this issue's scope.
- Terminal edits must continue using `outgoing_message` so HTML escaping, automatic truncation to Telegram's 4096-character limit, link-preview behavior, and reply-markup cleanup remain consistent with existing sends.
