# Codex plan for mehrangta/telegram_project_manager#96

Job: `c-cfd98d73` · Revision: 1

The close action currently deletes the Telegram “Issue created.” message and sends a new “Issue closed.” message. This produces duplicate lifecycle notifications and removes the original message users expect to reflect the issue’s state. Implement edit-in-place delivery by carrying the callback source message ID through the existing response abstraction, preserving current behavior for unrelated responses and manually typed close commands.

## Steps

1. **Add an edit-in-place response contract** — Extend `OutgoingMessage` with an optional edit target such as `edit_message_id: int | None`, and add the matching optional argument to `outgoing_message`. Keep the default `None` so every existing caller continues to send a new message. Define edit semantics as full message replacement: text, parse mode, link-preview setting, and keyboard are all replaced, with an empty keyboard explicitly serialized as `{"inline_keyboard": []}` to remove the original Plan, Code, Plan & Code, Issue, and Close buttons.
   - Likely files: `src/telegram_project_manager/platform/responses.py`, `tests/test_responses.py`
2. **Teach polling delivery to edit targeted messages** — Update `run_polling`’s `send_response` helper to call `TelegramBotApi.edit_message_text` when `OutgoingMessage.edit_message_id` is present and otherwise retain the existing `send_message` path. Pass `reply_markup(include_empty=True)` for edits so stale inline buttons are removed; do not pass thread or reply parameters because Telegram edits are addressed by chat ID and message ID. Treat Telegram’s “message is not modified” response as an idempotent success for repeated or concurrent callbacks. For other edit errors, log the chat and target message IDs and preserve the existing error propagation rather than automatically sending a second closure message, which would recreate the behavior this issue removes.
   - Likely files: `src/telegram_project_manager/platform/telegram_bot.py`, `tests/test_telegram_bot.py`
3. **Preserve the Close callback source message** — Change callback cleanup classification so `/confirm i-*` and `/cancel i-*` continue deleting their draft preview, while `/close i-*` is no longer deleted before dispatch. Keep Issue Plan/Code callback deletion unchanged. Continue populating `IncomingMessage.callback_source_message_id` from `CallbackAction.source_message_id`; this existing field supplies the exact bot-authored “Issue created.” message to edit, so no database column or migration is needed.
   - Likely files: `src/telegram_project_manager/platform/telegram_bot.py`, `tests/test_telegram_bot.py`
4. **Return an editable successful-close response** — Change `IssueManager.close` to return an `OutgoingMessage` on success, using the existing closed-message fields (`Issue closed.`, repo, issue number, title, and link) and setting its edit target from `message.callback_source_message_id`. Supply no replacement buttons so delivery clears the original action keyboard. Keep authorization, chat/topic validation, GitHub closure, database status transition, auditing, and service-level idempotency unchanged. If GitHub closure fails, retain the original issue-created message and return the existing separate error response so the Close button remains available for retry. If `/close i-*` is manually typed and has no callback source ID, preserve backward compatibility by sending the successful response normally rather than introducing message lookup,;
   - Likely files: `src/telegram_project_manager/bots/issue_manager/commands.py`, `tests/test_issue_manager.py`
5. **Validate behavior and rollout compatibility** — Add manager tests asserting that callback-driven closure returns the closed text with the callback source as its edit target and an empty keyboard, while direct-command closure has no edit target. Add polling tests asserting that a Close callback acknowledges the action, does not delete or send a message, edits the same source message, and clears its keyboard; retain assertions that confirm, cancel, and issue-code callbacks still delete their source messages. Add an idempotency test for “message is not modified” and ensure normal `OutgoingMessage` instances still use `sendMessage`. Correct the existing callback test’s stale reply-target expectation from four entries to six because its command fixture already contains six Plan/Code variants. No configuration, schema migration, backfill, or2
   - Likely files: `tests/test_responses.py`, `tests/test_issue_manager.py`, `tests/test_telegram_bot.py`

## Validation

- uv run python -m unittest tests.test_responses tests.test_issue_manager tests.test_telegram_bot
- uv run python -m unittest discover

## Risks

- GitHub is closed before Telegram is edited; a non-idempotent Telegram edit failure can leave the original message showing an active Close button even though the persisted issue status is closed. Logging must include chat and message IDs so this mismatch is diagnosable.
- Repeated or concurrent Close callbacks can attempt the same edit after the service’s idempotent close path returns; “message is not modified” must be accepted as success to avoid reporting a false failure.
- Manually typed `/close i-*` commands do not carry the original bot message ID and will retain the legacy separate-response behavior unless a future change persists issue-created Telegram message IDs.
- Adding edit semantics to the shared response type can affect all handlers if the default path changes accidentally; tests must prove that responses without an edit target still call `sendMessage` with unchanged reply behavior.
- Existing issue-created messages can use the new behavior immediately because callback updates include their source message IDs, but previously deleted or separately closed messages cannot be repaired retroactively.
- The current branch has a pre-existing targeted-test failure: `test_issue_plan_and_code_buttons_delete_created_message_then_dispatch` dispatches six commands but expects four reply-target values; the expectation should be synchronized with its fixture during the test update.
