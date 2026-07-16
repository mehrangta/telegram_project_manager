# Codex plan for mehrangta/telegram_project_manager#6

Job: `c-dac10c4e` · Revision: 1

Implement issue-preview cleanup in the Telegram callback layer without database or schema changes. Confirm and Cancel callbacks will remove the source issue-draft preview before dispatching their existing commands. Because the current Edit button uses Telegram's client-side copy_text action and produces no bot update, the recommended implementation converts only issue-draft Edit controls into an observable callback that removes the preview and sends a small reply target for feedback or images. Typed commands, reply-based editing, code-job controls, merge/deploy confirmation, private chats, and forum topics remain compatible.

## Steps

1. **Make Issue Edit Observable** — In keyboard generation, recognize only the placeholder command `/edit i-XXXXXXXX <feedback>` and render it as an `edit_issue:i-XXXXXXXX` callback button instead of a copy_text button. Keep the Draft ID copy button, Confirm and Cancel command callbacks, and copy behavior for other placeholder commands such as `/code edit c-XXXXXXXX <feedback>`. Validate the generated callback against Telegram's existing 64-byte limit.
   - Likely files: `src/telegram_project_manager/platform/responses.py`
2. **Add Safe Callback Deletion** — Add a small async helper inside polling that calls the existing `TelegramBotApi.delete_message(chat_id, message_id)` through `asyncio.to_thread`. Catch `TelegramBotApiError`, log the chat and message identifiers without callback contents or user data, and continue processing so a missing, expired, or undeletable Telegram message never suppresses the requested edit, confirmation, or cancellation action.
   - Likely files: `src/telegram_project_manager/platform/telegram_bot.py`
3. **Handle Edit Callback Flow** — Add a strict issue-draft ID pattern and handle `edit_issue:` before generic command callbacks. After admin authorization and ID validation, acknowledge the callback, best-effort delete the source preview, and send a replacement edit prompt in the same chat and `message_thread_id`. Include `Draft ID: i-XXXXXXXX` in the prompt so the existing reply parser sets `reply_to_draft_id`; tell the user to reply with feedback or images or use `/edit i-XXXXXXXX <feedback>`. Construct this prompt with an explicitly empty keyboard to prevent recursively generating another Edit/Confirm/Cancel preview. Invalid IDs receive the existing expired-button acknowledgement and do not delete anything.
   - Likely files: `src/telegram_project_manager/platform/telegram_bot.py`
4. **Delete On Confirm Or Cancel** — After parsing and validating a generic `command:` callback, identify only exact `/confirm i-XXXXXXXX` and `/cancel i-XXXXXXXX` issue-draft commands. Acknowledge the callback, best-effort delete its source preview, then dispatch the unchanged command through `TelegramRouter`. Do not delete previews for typed commands, unrelated callbacks, commit-plan confirmations, code-job actions, merge/deploy controls, invalid callbacks, or non-admin users. If the domain action later reports an expired, completed, unauthorized, or GitHub failure, keep the preview deleted as requested and send the existing result or error response; no preview restoration is required.
   - Likely files: `src/telegram_project_manager/platform/telegram_bot.py`, `src/telegram_project_manager/platform/router.py`, `src/telegram_project_manager/bots/issue_manager/commands.py`
5. **Cover Presentation Behavior** — Update response tests to assert that an issue preview retains the Draft ID copy action, emits `edit_issue:i-12345678` for Edit, and continues to emit the existing Confirm and Cancel command callbacks. Add a regression case proving `/code edit c-XXXXXXXX <feedback>` or another non-issue placeholder remains a copy action, preventing a global keyboard behavior change.
   - Likely files: `tests/test_responses.py`
6. **Cover Callback Lifecycle** — Extend the polling test bot with delete-message recording. Test Edit deletion plus creation of a thread-preserving, keyboard-free reply prompt containing the Draft ID; test Confirm and Cancel deletion plus normal router dispatch; assert a generic command callback is not deleted; retain the non-admin no-side-effects assertion; and simulate `delete_message` raising `TelegramBotApiError` to verify the action still dispatches and a warning is logged. Existing API tests already cover the exact `deleteMessage` payload.
   - Likely files: `tests/test_telegram_bot.py`
7. **Validate And Roll Out** — Run the focused response and polling suites first, then the full unittest suite. No migration, configuration update, or backfill is needed. After deployment, manually verify a created and revised issue preview in both a private chat and a forum topic: Edit removes the preview and produces a usable feedback reply target, while Confirm and Cancel remove the preview and return their normal result messages.
   - Likely files: `pyproject.toml`, `README.md`, `tests/test_responses.py`, `tests/test_telegram_bot.py`

## Validation

- PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests -p 'test_responses.py'
- PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests -p 'test_telegram_bot.py'
- PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests

## Risks

- Telegram copy_text buttons do not generate callback updates, so deletion at the moment of the current Edit click is technically impossible without changing the Edit control to a callback.
- The recommended Edit flow replaces the one-tap command-copy interaction with a bot prompt that users must reply to or follow with a typed `/edit` command.
- Deleting before command completion means a preview remains removed even when Confirm or Cancel returns a domain or GitHub error; this matches the issue's click-based requirement but removes the convenient retry buttons.
- Bots may lack deletion rights or Telegram may reject deletion for an old or already-removed message; deletion must remain best-effort so the underlying action still runs.
- Broad command matching could accidentally remove unrelated workflow messages; use strict issue-draft ID and exact-command matching rather than checking only `/confirm`, `/cancel`, or `/edit` prefixes.

## Open questions

1. How should the Edit control change, given Telegram does not notify the bot when the current copy_text button is clicked?
   A. Callback edit prompt: replace Edit with a callback that deletes the preview and sends a reply target for feedback or images. (Recommended) (recommended)
   B. Delete on submitted edit: retain the copy button and remove the preview only after feedback is submitted, which requires tracking preview message IDs and does not delete on click.
   C. Confirm/cancel only: leave Edit unchanged and delete only for observable Confirm and Cancel callbacks, which does not fully satisfy the issue wording.
