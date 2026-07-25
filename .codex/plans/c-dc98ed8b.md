# Codex plan for mehrangta/telegram_project_manager#20

Job: `c-dc98ed8b` · Revision: 1

Change the inline action shown after a GitHub issue is created from “💻 Code” to “📝 Plan”. Keep its callback as `/code <repo>#<issue>` without `--skip-plan`, because the existing code-job flow already queues the Plan phase first, waits for clarification or approval, and only enters Code after approval. This is a presentation and regression-test change; it requires no new command, schema, storage migration, configuration, or documentation update.

## Steps

1. **Relabel the created-issue action** — In `IssueManager.confirm`, change the first inline button label from `💻 Code` to `📝 Plan`. Preserve the existing callback command `/code owner/repository#number`, including the short `#number` fallback when Telegram’s 64-byte callback-data limit would be exceeded. Do not add `--skip-plan` or introduce a new `/plan` command.
   - Likely files: `src/telegram_project_manager/bots/issue_manager/commands.py`
2. **Lock the Telegram interface contract** — Rename the created-issue button test to describe the Plan action and assert all user-visible and routing behavior: the first button text is exactly `📝 Plan`, its callback remains `command:/code owner/repo#12`, and the second button still opens the GitHub issue URL. The exact callback assertion also prevents accidentally bypassing planning with `--skip-plan`.
   - Likely files: `tests/test_issue_manager.py`
3. **Preserve the existing phase data flow** — Leave `CodeManager._start` and `CodeJobService.create_job` unchanged. A click is routed as a normal `/code` command; with no `--skip-plan`, `skip_plan` remains false, the job is created with `status=queued_plan` and `resume_phase=plan`, and Code is queued only after the plan reaches `awaiting_approval` and `/code approve` succeeds. Existing failures—invalid or disallowed repository, unavailable repository cache, GitHub issue lookup failure, full queue, or an already-active job—continue through the current error responses.
   - Likely files: `src/telegram_project_manager/bots/code_manager/commands.py`, `src/telegram_project_manager/bots/code_manager/service.py`
4. **Validate and roll out** — Run the focused issue-manager tests, then the complete unittest suite. Verify the generated reply markup contains the new label and unchanged callback. Deploy as a normal application update with no database work; newly sent issue confirmations receive the Plan label, while previously sent Telegram messages retain their old Code label but still invoke the same plan-first callback.
   - Likely files: `tests/test_issue_manager.py`, `tests/test_code_manager.py`

## Validation

- Run `uv run python -m unittest tests/test_issue_manager.py` and confirm the created-issue response exposes `📝 Plan`, invokes `command:/code owner/repo#12`, and retains the issue URL button.
- Run `uv run python -m unittest tests/test_code_manager.py` to preserve the existing contract that `skip_plan=False` reaches `awaiting_approval` before Code runs.
- Run `uv run python -m unittest discover -s tests` for full regression coverage.
- The planning environment could not execute these commands because `uv` and the locked dependency `langchain_openai` are not installed; use the repository’s normal `uv` environment during implementation validation.

## Risks

- Changing the callback to a hypothetical `/plan` command would break routing because only `/code` starts code jobs; only the button label should change.
- Removing the qualified-reference fallback could exceed Telegram’s 64-byte callback-data limit for long repository names.
- Previously delivered Telegram messages are not automatically edited, so their button continues to display Code until a new issue confirmation is sent; behavior remains plan-first.
- The explicit manual `/code --skip-plan` escape hatch remains available for existing users, but the issue-created Plan button must never include it.
