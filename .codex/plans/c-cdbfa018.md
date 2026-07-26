# Codex plan for mehrangta/telegram_project_manager#47

Job: `c-cdbfa018` · Revision: 1

Align brainstorm generation with the existing single-message Telegram presentation contract so all three ideas contain concise, complete text instead of being silently cut mid-sentence. The current schema permits 650 characters for every detail, while the renderer truncates opportunity, proposal, and value to 240, 280, and 200 characters; the implementation should move those actual presentation limits into the structured-output contract, reject invalid oversized responses, render validated content without shortening, and fail explicitly rather than letting the shared Telegram formatter truncate a result.

## Steps

1. **Define the complete-result schema** — Replace the shared 650-character detail limit with field-specific limits matching the proven single-message layout: title 100, opportunity 240, proposal 280, value 200, at most three sources, and 90 characters per source. Apply these constants directly to BRAINSTORM_RESPONSE_SCHEMA so Codex generates content for the real delivery budget. Normalize detail whitespace during BrainstormResponse.from_json, retain exactly three unique titles and source deduplication, and replace every silent string slice with an explicit ValueError when a returned field exceeds its declared limit. Reject detail text ending in an ellipsis so an incomplete model response cannot be presented as successful.
   - Likely files: `src/telegram_project_manager/bots/ideas/schemas.py`
2. **Request concise complete ideas** — Update the brainstorm prompt to define the purpose of each existing field without changing the response interface: opportunity should cite the repository-backed product gap, proposal should describe the user-visible workflow and key implementation surfaces, and value should explain impact, repository fit, and confidence. Require complete standalone sentences within schema limits, prohibit ellipses and sentence fragments, and tell Codex not to include ranking numbers in titles because the renderer supplies numbering. Preserve the existing focus on new capabilities rather than bugs, maintenance, or generic advice.
   - Likely files: `src/telegram_project_manager/bots/ideas/prompts.py`
3. **Render validated content verbatim** — Remove _shorten calls from _render_result for titles, details, and sources; the schema parser becomes the sole content-length boundary, so successful output is never altered after generation. Keep the existing repository metadata and Opportunity, Proposal, Value, and Sources labels. After assembling the complete plain-text result, compare its length with TELEGRAM_TEXT_LIMIT before calling outgoing_message; raise a descriptive ValueError if metadata or any unexpected condition pushes the result over Telegram's 4096-character limit. This prevents outgoing_message from replacing the end of idea 3 with its generic truncated marker.
   - Likely files: `src/telegram_project_manager/bots/ideas/service.py`, `src/telegram_project_manager/platform/responses.py`
4. **Preserve delivery and failure semantics** — Keep the behavior introduced for issue 33: a manual run sends one queued acknowledgement and edits that same message with the terminal result, while a scheduled run sends one terminal message. Let schema and message-budget ValueError failures follow the existing service failure path, update brainstorm_configs.last_status and last_error, audit the failed result, and edit the manual queued message with the safe failure explanation. Do not fall back to partial content or multiple idea messages, and do not change queueing, concurrency, scheduling, worktree, Codex model, database, or command interfaces.
   - Likely files: `src/telegram_project_manager/bots/ideas/service.py`, `src/telegram_project_manager/bots/ideas/commands.py`, `src/telegram_project_manager/platform/storage/db.py`
5. **Add regression coverage** — Extend BrainstormSchemaTests with boundary-value cases for each field, rejection of oversized details and sources, rejection of trailing ellipses, preservation of normalized complete text, and the existing exactly-three and unique-title requirements. Add renderer tests using near-maximum valid ideas and assert that all three complete final sentences and source paths remain present, no Unicode ellipsis or '... truncated ...' marker is introduced, and the assembled plain text stays within TELEGRAM_TEXT_LIMIT. Extend service tests to confirm manual delivery remains one send plus one edit, scheduled delivery remains one send, complete detail endings reach Telegram unchanged, and an over-budget rendering error produces failed status rather than a partial result.
   - Likely files: `tests/test_brainstorm.py`, `tests/test_responses.py`
6. **Document and roll out** — Update the ideas bot documentation to state that brainstorm fields are generated to a bounded single-message format and are delivered complete rather than post-generation truncated. No database migration, configuration change, command change, or backfill is required; deployment only requires restarting the bot process. After rollout, verify one manual and one scheduled brainstorm and monitor brainstorm.result audit failures and brainstorm_configs.last_error for providers that do not honor the structured schema.
   - Likely files: `src/telegram_project_manager/bots/ideas/README.md`, `README.md`

## Validation

- Run `uv run python -m unittest discover -s tests -p 'test_brainstorm.py'` for schema, rendering, service, scheduling, and delivery regressions.
- Run `uv run python -m unittest discover -s tests -p 'test_responses.py'` for Telegram formatting and truncation-boundary coverage.
- Run `uv run python -m unittest discover -s tests` for the complete repository regression suite.
- Manually run `/brainstorm` in an enabled chat or topic and verify the queued message is edited in place, contains three complete ideas, has no mid-sentence ellipses, and remains within Telegram's message limit.
- Trigger or wait for one scheduled brainstorm and verify it posts one complete terminal message and advances the configured schedule.

## Risks

- Tighter structured-output limits may cause nonconforming Codex-compatible providers to return validation failures instead of the partial messages users previously received; this is intentional and should be observable through the existing failure message, audit record, and last_error state.
- Character limits do not by themselves guarantee high-quality prose, so the prompt and regression fixtures must emphasize repository evidence and complete sentences rather than merely shorter wording.
- Very long repository or branch metadata could consume the remaining Telegram budget even when every idea satisfies its schema; the explicit final budget check must fail safely instead of truncating idea 3.
- Changing the shared outgoing_message truncation behavior would affect unrelated bot features, so the brainstorm-specific preflight check should be added without altering global truncation semantics.
- The planning environment did not contain `uv` or `python`, so the identified unittest commands could not be executed during planning.
