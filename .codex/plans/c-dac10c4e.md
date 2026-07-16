# Codex plan for mehrangta/telegram_project_manager#6

Job: `c-dac10c4e` · Revision: 2

Implement issue-draft preview cleanup entirely in the Telegram presentation and callback layers, with no database, schema, router, or issue-manager behavior changes. Apply the authorized Edit UX: replace the current client-side copy button with an observable callback that creates a reply target for feedback or images and then removes the old preview. Confirm and Cancel callbacks remove the issue preview before dispatching their existing commands. Typed `/edit`, `/confirm`, and `/cancel` commands, reply-based editing, code-job controls, merge/deploy controls, private chats, and forum topics remain compatible.

## Steps

1. **Make Issue Edit Observable** — Update `keyboard_for_text` to recognize only an issue-draft placeholder matching `/edit i-XXXXXXXX <feedback>`. Render it as a normal callback button labeled for Edit with callback data `edit_issue:i-XXXXXXXX` instead of a `copy_text` button. Retain the Draft ID copy button and the existing `command:/confirm i-XXXXXXXX` and `command:/cancel i-XXXXXXXX` callbacks. Keep all non-issue placeholder commands, including `/code edit c-XXXXXXXX <feedback>`, as copy actions. The callback remains well below Telegram's enforced 64-byte callback-data limit.
   - Likely files: `src/telegram_project_manager/platform/responses.py`
2. **Add Best-Effort Deletion** — Inside `run_polling`, add a callback-source deletion helper that invokes the existing `TelegramBotApi.delete_message(chat_id, source_message_id)` through `asyncio.to_thread`. Catch `TelegramBotApiError`, log a warning with only the chat and message identifiers, and return without raising. Deletion failures caused by missing permissions, message age, duplicate updates, or an already-deleted message must never prevent the selected action from continuing.
   - Likely files: `src/telegram_project_manager/platform/telegram_bot.py`
3. **Implement Edit Callback Prompt** — Add a strict callback draft-ID pattern for `i-` followed by eight lowercase hexadecimal characters and handle the `edit_issue:` prefix before generic `command:` callbacks. Reject malformed IDs with the existing `Button expired` acknowledgement and no message changes. For a valid admin callback, acknowledge that edit feedback is requested, create a standalone `OutgoingMessage` in the callback's existing chat and `message_thread_id`, and explicitly pass an empty keyboard so the prompt does not regenerate issue action buttons. The prompt must contain a line exactly matching `Draft ID: i-XXXXXXXX` plus instructions to reply with feedback or images or run `/edit i-XXXXXXXX <feedback>`; this lets the existing `_reply_to_draft_id` parser and `IssueManager.handle` route replies without new state.送
   - Likely files: `src/telegram_project_manager/platform/telegram_bot.py`
4. **Replace Preview Safely** — For Edit, send the replacement prompt first and delete the original preview only after the prompt is accepted by Telegram. This ordering preserves the existing preview if prompt delivery fails, while successful clicks leave only the new feedback target. The deletion remains best-effort, so a failed deletion may temporarily leave both messages but does not invalidate the new edit flow. Preserve the callback's topic identifier when sending the prompt.
   - Likely files: `src/telegram_project_manager/platform/telegram_bot.py`
5. **Delete On Confirm And Cancel** — After extracting and validating a generic `command:` callback, use a strict full-match check for only `/confirm i-XXXXXXXX` and `/cancel i-XXXXXXXX`. After callback acknowledgement, best-effort delete the source preview and then construct the existing command-backed `IncomingMessage` and dispatch it through `TelegramRouter`. Do not delete messages for typed commands, unrelated callbacks, plan confirmations, code-job actions, merge/deploy actions, malformed callback data, or non-admin users. Domain authorization and state checks remain in the existing issue manager and executor. If confirmation or cancellation subsequently returns an expired, completed, wrong-chat, unauthorized, GitHub, or Telegram error response, do not recreate the deleted preview.
   - Likely files: `src/telegram_project_manager/platform/telegram_bot.py`
6. **Update Presentation Tests** — Revise the issue-preview keyboard assertion so the only copied value is the Draft ID and the callback list contains `edit_issue:i-12345678`, `command:/confirm i-12345678`, and `command:/cancel i-12345678` in rendered order. Add a regression test using a code-job message with `/code edit c-XXXXXXXX <feedback>` to prove non-issue placeholder commands remain copy buttons and are not converted to `edit_issue:` callbacks.
   - Likely files: `tests/test_responses.py`
7. **Test Callback Lifecycle** — Extend the polling test bot with a `delete_message` method and a list of deleted `(chat_id, message_id)` pairs. Add tests that a valid Edit callback sends a keyboard-free prompt containing the exact Draft ID in the original topic and then deletes the preview; that the prompt text is recognized by `incoming_message_from_update` as a bot reply target; and that malformed Edit callbacks are acknowledged as expired without sending or deleting anything. Add Confirm and Cancel callback cases asserting deletion and unchanged router command dispatch. Assert generic code callbacks and merge/deploy flows retain their existing behavior and are not deleted by the issue-specific matcher.
   - Likely files: `tests/test_telegram_bot.py`
8. **Test Failure And Permission Cases** — Add a fake bot whose `delete_message` raises `TelegramBotApiError` and verify Confirm or Cancel still dispatches, Edit still supplies its prompt, and a warning is logged. Extend the existing non-admin callback test to assert no deletions occur. Preserve the existing expired callback acknowledgement test, which confirms an acknowledgement failure does not block callback execution or later updates. Keep the existing low-level API assertion for the exact `deleteMessage` payload.
   - Likely files: `tests/test_telegram_bot.py`

## Validation

- PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests -p 'test_responses.py'
- PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests -p 'test_telegram_bot.py'
- PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests
- Manual: create and revise an issue draft in a private chat; verify Edit replaces the preview with a reply target and replies still revise the same draft.
- Manual: repeat in a forum topic; verify the Edit prompt and action responses stay in the original topic.
- Manual: verify Confirm and Cancel immediately remove their preview and still return the existing success or error response.

## Risks

- The authorized Edit behavior intentionally replaces the current one-tap command-copy interaction with a callback followed by a reply prompt; users can still type the documented `/edit` command directly.
- Telegram message deletion depends on bot permissions and Telegram retention rules, so cleanup is best-effort and the old preview may remain when the API rejects deletion.
- Issue Confirm and Cancel previews are removed before domain processing completes; an invalid or failed action therefore leaves only the resulting error message rather than restoring retry buttons.
- Any registered admin able to press the inline action can trigger preview deletion before existing author or draft-state checks reject the underlying operation; this follows the click-based requirement and existing admin callback authorization boundary.
- An overly broad matcher could delete unrelated workflow messages, so both the Edit prefix and Confirm/Cancel commands must require a strict issue-draft ID full match.
- Sending the Edit prompt before deletion avoids losing the only edit instructions when Telegram rejects the new message, but users may briefly see both messages and may permanently see both if deletion fails.
