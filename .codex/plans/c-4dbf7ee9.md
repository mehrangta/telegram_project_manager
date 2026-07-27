# Codex plan for mehrangta/telegram_project_manager#61

Job: `c-4dbf7ee9` · Revision: 1

Draft pull-request plans currently start with a generic plan announcement, and the generated `summary` may lead with implementation language rather than the underlying problem. Reviewers therefore can reach proposed work before receiving two clear sentences that explain what is wrong and why it matters. Update the planner output contract and PR-body ordering so the first two prose sentences are problem-only for both initial plans and revisions, while retaining the existing `CodePlan` JSON shape and downstream workflow.

## Steps

1. **Define the problem-first planner contract** — Update both `planning_prompt` and `plan_edit_prompt` to require the `summary` to open with exactly two complete sentences describing only the current problem and its impact or context. Explicitly prohibit proposed solutions, implementation steps, file changes, validation, or recommendations in those two sentences; allow implementation framing only from the third sentence onward. Apply the same rule during revisions so authorized feedback cannot accidentally restore a solution-first opening.
   - Likely files: `src/telegram_project_manager/bots/code_manager/prompts.py`
2. **Reinforce the structured-output schema** — Add a descriptive constraint to the existing `CODE_PLAN_SCHEMA.properties.summary` field documenting the two problem-only opening sentences. Keep the field name, required properties, `CodePlan` dataclass, serialized `plan_json`, and database representation unchanged, avoiding a migration and preserving compatibility with stored plans. Do not introduce brittle runtime sentence parsing or reject plans based on punctuation because the semantic distinction between problem and solution cannot be validated reliably by a parser.
   - Likely files: `src/telegram_project_manager/bots/code_manager/schemas.py`
3. **Remove solution-neutral prose before the summary** — Change `_plan_pr_body` so it no longer prepends the sentence `Draft implementation plan for ...` before the rendered plan. Start the body with the existing plan Markdown, whose heading and job metadata are not prose sentences, making `CodePlan.summary` the first prose paragraph. Preserve the `Refs #<issue>` footer and `telegram-code-job` marker so issue association, automation lookup, draft creation, and subsequent PR updates continue to work.
   - Likely files: `src/telegram_project_manager/bots/code_manager/service.py`
4. **Cover initial and revised PR rendering** — Update the representative plan fixture to contain two problem-only opening sentences, then assert that initial draft PR creation places those sentences before any implementation-oriented step text and excludes the old generic preamble. Extend the plan-feedback revision test to verify the updated PR body follows the same ordering, ensuring both `create_draft_pr` and `update_pr` receive compliant content. Add focused assertions that the initial and revision prompts, plus the schema description, retain the contract.
   - Likely files: `tests/test_code_manager.py`
5. **Verify compatibility and rollout behavior** — Confirm `CodePlan.from_json`, `to_json`, progress rendering, coding prompts, question handling, and `_ready_pr_body` continue consuming the unchanged `summary` field. Existing stored plans remain readable; newly generated plans and edited plans adopt the new opening immediately, while already-open draft PRs are not backfilled until their plan is revised. No database migration, configuration change, feature flag, or deployment sequencing is required.
   - Likely files: `src/telegram_project_manager/bots/code_manager/schemas.py`, `src/telegram_project_manager/bots/code_manager/progress.py`, `src/telegram_project_manager/bots/code_manager/service.py`, `tests/test_code_manager.py`

## Validation

- Run `uv run python -m unittest tests.test_code_manager.CodeSafetyTests` to validate the initial/revision prompt contract, schema metadata, normalization, and Markdown rendering.
- Run `uv run python -m unittest tests.test_code_manager.CodeJobServiceTests.test_plan_approval_runs_code_and_marks_draft_pr_ready tests.test_code_manager.CodeJobServiceTests.test_open_questions_block_approval_and_telegram_answer_revises_main_plan` to validate initial draft creation and revised PR-body updates.
- Run `uv run python -m unittest discover -s tests` for the complete repository regression suite.
- Run `git diff --check` to catch whitespace errors. The planning container did not provide `uv` or a Python executable, so these repository-appropriate commands were identified from `pyproject.toml`, `uv.lock`, and the unittest-based test layout but were not executed during planning.

## Risks

- The problem-versus-solution distinction is semantic and cannot be guaranteed mechanically without unreliable natural-language parsing; reinforce it in both prompts and the structured schema description, and lock the expected output ordering with representative tests.
- Removing the existing PR-body preamble changes draft-plan formatting. Preserve the issue reference footer and automation marker, and assert their presence so GitHub linking and job identification do not regress.
- Existing open draft PRs will retain their current text until a plan revision rewrites the body; automatic backfilling would add unrelated remote-update behavior and is outside this issue's scope.
