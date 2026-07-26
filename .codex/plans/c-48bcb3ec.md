# Codex plan for mehrangta/telegram_project_manager#23

Job: `c-48bcb3ec` · Revision: 1

Implement a shared, bounded recovery path in `OpenAICompatibleClient.chat_json` for intermittent structured responses whose envelope is valid but whose `parsed` value is missing or not an object. First recover an object already present in the raw `AIMessage`; otherwise retry the same structured invocation once. Preserve current prompts, schemas, user-facing errors, persistent-memory semantics, and downstream domain validation. No product decision or repository/schema migration is required, and no files were modified while preparing this plan.

## Steps

1. **Centralize structured-object extraction** — Add private helpers in the LLM client to normalize Pydantic-like values through `model_dump()` and accept only dictionary results. Inspect candidates in this order: the response envelope's `parsed` value, `raw.additional_kwargs["parsed"]` when `raw` is an `AIMessage`, and JSON decoded from `raw.text`. Use `AIMessage.text` rather than only string-valued `raw.content` so OpenAI-compatible providers returning text content blocks are supported. Reuse `parse_json_object` for raw text so fenced JSON remains compatible and non-object JSON is rejected. Do not include raw model output in exceptions or audit data.
   - Likely files: `src/telegram_project_manager/platform/llm/client.py`
2. **Add one semantic response retry** — Prepare the structured runnable and invocation input once, including one snapshot of persistent history, then invoke it in a two-attempt loop. A non-dictionary response envelope remains an immediate `LLM structured response is invalid` failure. A truthy `parsing_error` remains an immediate `LLM returned invalid structured output` failure. When parsing reports no error but no dictionary can be recovered, repeat the identical invocation once without sleeping; this addresses transient `null`, scalar, array, empty, or provider-incomplete structured results that transport-level `max_retries` does not cover. If the second result is still unrecoverable, raise the existing `LLM structured response missing parsed object` message so Telegram behavior and existing audit classification remain stable.
   - Likely files: `src/telegram_project_manager/platform/llm/client.py`
3. **Preserve successful memory semantics** — Append the human/AI exchange only after an object has been recovered. Never persist the failed first attempt. For the successful attempt, store its raw textual output when available; otherwise store canonical JSON serialized from the recovered dictionary. This ensures future issue or commit prompts see one successful exchange rather than a transient invalid response or duplicate user messages.
   - Likely files: `src/telegram_project_manager/platform/llm/client.py`, `src/telegram_project_manager/platform/llm/memory.py`
4. **Cover direct raw recovery cases** — Extend `LlmClientTests` with responses where top-level `parsed` is `None` but the raw message contains a dictionary in `additional_kwargs`, a JSON object in string content, or a JSON object in a text content block. Assert that each returns the dictionary with only one provider invocation and continues to honor caller-supplied schemas.
   - Likely files: `tests/test_llm_client.py`
5. **Cover retry and terminal failures** — Add a regression test where the first invocation returns `parsed=None` with raw JSON `null` and the second returns a valid object; assert two invocations and the successful result. Add exhaustion coverage where both attempts return non-object JSON and assert the existing missing-parsed-object error. Also assert that malformed-output `parsing_error` and invalid response envelopes are not semantically retried, preserving their distinct error messages.
   - Likely files: `tests/test_llm_client.py`
6. **Validate memory and shared compatibility** — Add a memory-enabled retry test confirming that only the successful exchange is stored and replayed. No issue-manager-specific production change is needed because generated issue drafts, title-only drafts, issue revisions, and commit plans all call the shared client; their existing `IssueDraft` and `CommitPlan` validation remains the final domain boundary. Run the focused client tests, then the complete unittest suite and whitespace validation. Roll out without a feature flag or database migration, and monitor existing `issue.plan`, `issue.revise`, and `plan.create` failure audits for the exact missing-parsed-object reason.
   - Likely files: `tests/test_llm_client.py`, `src/telegram_project_manager/bots/issue_manager/planner.py`, `src/telegram_project_manager/bots/commit_manager/planner.py`

## Validation

- uv run python -m unittest discover -s tests -p 'test_llm_client.py'
- uv run python -m unittest discover -s tests
- git diff --check

## Risks

- A semantic retry can add one model request, increasing latency and cost only when the first structured result is unrecoverable; keep the limit at two total invocations and retain the existing provider transport retry configuration.
- If a provider consistently returns `null` or another non-object despite the JSON schema, the request still fails after the bounded retry with the same user-visible reason; the fix intentionally avoids unbounded retries or fabricated defaults.
- Raw JSON recovery accepts a dictionary when the provider failed to populate LangChain's parsed field. The current dictionary-schema path does not perform independent local JSON Schema validation, so existing `IssueDraft`, title, and `CommitPlan` validation must remain unchanged and must run before persistence.
- Retry preparation must reuse the same history snapshot and must not append the failed attempt; rebuilding history or persisting both attempts could duplicate conversation turns and make later generations less predictable.
- The repository expects `uv` for execution, but `uv` and project dependencies were unavailable in the planning environment, so the listed validation commands must be run in the implementation environment.
