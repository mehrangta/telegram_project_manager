# Codex plan for mehrangta/telegram_project_manager#38

Job: `c-72b6de3f` · Revision: 1

Add a third inline action to the post-creation “Issue created” message: keep “📝 Plan” as the existing Plan-first path, add “💻 Code” to invoke the already-supported `/code --skip-plan` path, and retain “↗ Issue”. Reuse the current command, callback dispatch, permissions, job persistence, and immediate coding workflow; no database, schema, configuration, or Telegram platform changes are required. No material product decision remains.

## Steps

1. **Centralize issue action callbacks** — In the issue confirmation response, replace the single inline Plan callback construction with a small private callback builder that accepts the created repository, issue number, and whether planning should be skipped. Generate `command:/code owner/repo#12` for Plan and `command:/code --skip-plan owner/repo#12` for Code. Preserve Telegram’s 64-byte callback limit by falling back to the active-scope reference forms `command:/code #12` and `command:/code --skip-plan #12` when the repository-qualified payload is too long. Keep the full repository reference whenever it fits so a later active-repository change cannot redirect the action.
   - Likely files: `src/telegram_project_manager/bots/issue_manager/commands.py`
2. **Render Plan Code Issue actions** — Update the successful `IssueManager.confirm` keyboard to contain one row ordered as “📝 Plan”, “💻 Code”, and “↗ Issue”. The Plan button must retain the current behavior and omit `--skip-plan`; the Code button must include it; the Issue button must continue opening the GitHub issue URL. Do not change the issue-created text, draft execution, source-message retention, or failure response from issue creation.
   - Likely files: `src/telegram_project_manager/bots/issue_manager/commands.py`
3. **Reuse immediate coding workflow** — Do not add a new callback type or job state. Telegram’s generic `command:` callback handler already preserves the originating chat and topic, verifies the user is an admin, acknowledges the callback, and dispatches the generated `/code` command. `CodeManager._start` already removes `--skip-plan`, resolves the issue reference, validates the allowed repository, fetches the issue, and calls `CodeJobService.create_job(skip_plan=True)`. The service already creates a `queued_code` job with resume phase `code`, bypasses plan generation and approval, and proceeds through the normal workspace, Codex, validation, pull-request, reporting, recovery, and error paths.
   - Likely files: `src/telegram_project_manager/platform/telegram_bot.py`, `src/telegram_project_manager/bots/code_manager/commands.py`, `src/telegram_project_manager/bots/code_manager/service.py`, `src/telegram_project_manager/platform/storage/db.py`
4. **Add focused regression coverage** — Rename and expand the issue-manager success test to assert all three buttons, their order, labels, Plan callback, skip-plan Code callback, and unchanged issue URL. Add a long repository-name case proving both callback payloads stay within Telegram’s 64-byte limit and use the short issue-number fallback. Add a command-layer test using fake GitHub and service collaborators to dispatch `/code --skip-plan owner/repo#12` and verify the resolved issue is passed to `create_job` with `skip_plan=True`, the originating chat/topic metadata, default branch, and local repository path. Retain the existing service test that proves skip-plan jobs execute coding immediately, produce no plan JSON, and do not send a plan-approval notification; no new Telegram callback-handler test is necessary because the `‌
   - Likely files: `tests/test_issue_manager.py`, `tests/test_code_manager.py`
5. **Document the action choice** — Update the concise Issues and Code documentation to state that, after confirming an issue draft, Plan starts the existing plan-and-approval workflow while Code starts coding immediately by skipping the plan phase. Document `/code --skip-plan #123` alongside the existing `/code #123` form without changing default `/code` semantics.
   - Likely files: `README.md`

## Validation

- Run `uv sync` to install the locked project and test dependencies.
- Run `uv run python -m unittest tests.test_issue_manager tests.test_code_manager tests.test_telegram_bot` for focused issue-action, skip-plan, and callback-dispatch coverage.
- Run `uv run python -m unittest discover -s tests` for the complete regression suite.
- Manually create and confirm an issue draft in a Telegram test chat/topic; verify Plan displays the plan approval flow, Code starts a code job without a plan notification, Issue opens the correct GitHub URL, and all responses remain in the originating topic.
- Baseline tests could not be executed in the inspected environment because `uv` is not installed; direct Python execution was also blocked by the absent locked dependency `langchain_openai`.

## Risks

- Telegram limits callback data to 64 UTF-8 bytes. Both Plan and Code callbacks need explicit fallback coverage, especially because `--skip-plan` consumes additional bytes.
- The short `#number` fallback resolves against the chat/topic’s current active repository. This is inherited from the existing Plan implementation and can target the wrong repository only if an unusually long repository name forces fallback and the active repository changes before the button is pressed.
- Code intentionally removes the plan review and approval gate. Existing admin authorization, repository allow-list checks, workspace validation, change-size limits, Codex safeguards, validation, and pull-request flow must remain unchanged.
- Repeated button presses can request multiple code jobs under the existing `/code` behavior. Do not introduce confirmation or idempotency semantics as part of this narrowly scoped UI change.
- A three-button row may render more compactly on narrow Telegram clients, but the short existing labels avoid changing the requested interaction or splitting related actions across rows.
