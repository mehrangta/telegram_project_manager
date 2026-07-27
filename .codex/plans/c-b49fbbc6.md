# Codex plan for mehrangta/telegram_project_manager#58

Job: `c-b49fbbc6` · Revision: 1

Update the shared LLM client so an otherwise valid structured-response envelope with no recoverable parsed object triggers one schema-guided repair request instead of repeating the identical structured invocation. Keep the existing chat_json interface, maximum of two provider calls, caller-side domain validation, user-visible error contract, and memory behavior. No database, configuration, prompt-schema, or deployment migration is required.

## Steps

1. **Retain the base chat model** — Refactor OpenAICompatibleClient.chat_json to keep the configured ChatOpenAI instance separate from its with_structured_output runnable. Preserve the existing model, API key, base URL, temperature, timeout, retry configuration, response_schema argument, and public return type. Build one canonical message sequence containing the system prompt, persisted history when present, and current user prompt so both the primary and repair requests use identical conversation context.
   - Likely files: `src/telegram_project_manager/platform/llm/client.py`
2. **Preserve primary structured extraction** — Run the normal method="json_schema", include_raw=True request first. Continue accepting dictionaries from response["parsed"], model_dump-capable parsed values, raw AIMessage additional_kwargs["parsed"], and JSON objects recovered from raw text or text content blocks. Preserve immediate failure for provider exceptions, non-dictionary structured envelopes, and explicit parsing_error values; these cases should not enter the missing-object repair path.
   - Likely files: `src/telegram_project_manager/platform/llm/client.py`
3. **Add a bounded repair request** — When the primary response is a valid envelope with parsing_error unset but no recoverable object, invoke the retained base ChatOpenAI model once without structured-output wrapping. Append a retry-only human instruction stating that the previous result contained no JSON object, requiring exactly one non-null JSON object, and embedding the active response schema as compact JSON. Parse the returned AIMessage with the existing raw-text and parse_json_object helpers, including fenced JSON and text-block support. Replace the current identical two-attempt structured loop so the total provider-call ceiling remains two.
   - Likely files: `src/telegram_project_manager/platform/llm/client.py`
4. **Keep failure and memory semantics** — If the repair invocation raises, wrap it as LlmError("LLM request failed: ...") consistently with the primary call. If its content is empty, null, malformed JSON, or a non-object JSON root, finish with the existing "LLM structured response missing parsed object" error so issue-manager and commit-manager responses and audit behavior remain compatible. Do not persist the failed primary response or the repair instruction. On repair success, persist only the original user prompt and successful assistant JSON, then return the repaired dictionary for the existing IssueDraft or CommitPlan validation and storage flow.
   - Likely files: `src/telegram_project_manager/platform/llm/client.py`, `src/telegram_project_manager/bots/issue_manager/planner.py`, `src/telegram_project_manager/bots/commit_manager/planner.py`
5. **Expand focused client tests** — Update the retry tests to model one structured request followed by one base-model repair request. Cover successful repair from plain JSON, schema inclusion in the repair instruction, unchanged one-call recovery from raw structured content, exhausted repair with empty/null output, malformed and non-object repair output, repair provider exceptions, no repair for explicit parsing_error or invalid envelopes, and exactly two calls in the exhausted case. Adapt the memory regression test to prove only the successful repaired response is stored and replayed on the next request.
   - Likely files: `tests/test_llm_client.py`
6. **Verify caller compatibility** — Run issue-draft and commit-plan tests to confirm repaired dictionaries still pass through their existing domain validators before any database write. Confirm title-only issue mode, generated issue mode, revisions, and commit planning require no interface changes. Keep the current Telegram failure messages and audit event names unchanged; successful repairs should be indistinguishable from normal structured successes to callers.
   - Likely files: `tests/test_repository_context.py`, `tests/test_issue_manager.py`, `tests/test_planner_schema.py`, `src/telegram_project_manager/bots/issue_manager/commands.py`, `src/telegram_project_manager/bots/commit_manager/commands.py`
7. **Roll out without migration** — Deploy as a code-only reliability fix with no settings, schema migrations, or dependency changes. The repair path activates only after the reported missing-object condition, and replacing the existing duplicate retry keeps worst-case request count unchanged. After focused validation, run the full unittest suite before deployment and monitor existing issue.plan, issue.revise, and commit-plan failure audits for recurrence of the same error text.
   - Likely files: `pyproject.toml`, `uv.lock`

## Validation

- uv run python -m unittest discover -s tests -p 'test_llm_client.py'
- uv run python -m unittest discover -s tests -p 'test_repository_context.py'
- uv run python -m unittest discover -s tests -p 'test_issue_manager.py'
- uv run python -m unittest discover -s tests -p 'test_planner_schema.py'
- uv run python -m unittest discover -s tests

## Risks

- The repair request is prompt-constrained rather than provider-enforced JSON Schema output, so it must remain a single bounded fallback and must not bypass the existing IssueDraft and CommitPlan validation before persistence.
- OpenAI-compatible providers expose varied AIMessage content shapes; the repair path should reuse the existing centralized text extraction and JSON parsing helpers rather than introducing provider-specific branches.
- Refactoring prompt construction could accidentally omit or duplicate persisted history. Tests must compare the next-turn message sequence and verify the retry-only repair instruction is never written to memory.
- Embedding the full response schema increases only the fallback prompt size. This is acceptable for the current schemas but should use compact JSON serialization and avoid adding a new configurable retry count or dependency.
- The planning environment did not provide the repository's documented uv executable, so the listed commands were identified from repository conventions but were not executed during planning.
