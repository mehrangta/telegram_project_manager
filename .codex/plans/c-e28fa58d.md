# Codex plan for mehrangta/telegram_project_manager#36

Job: `c-e28fa58d` · Revision: 1

Increase the process-local `/code` execution limit from two to four concurrent jobs. Implement this as a named service default, preserve the existing constructor override and ten-job waiting-queue cap, and leave `/ask`, `/brainstorm`, and `/do` concurrency unchanged. No configuration key, database migration, command/API change, or documentation update is required.

## Steps

1. **Define the four-job default** — Add `MAX_CONCURRENT_CODE_JOBS = 4` beside `MAX_QUEUED_JOBS` and change `CodeJobService.__init__` to default `max_concurrent` to that constant. Retain the parameter so tests and custom callers can explicitly select another limit. Do not change `MAX_QUEUED_JOBS = 10`; it remains the admission cap for jobs in durable queued statuses.
   - Likely files: `src/telegram_project_manager/bots/code_manager/service.py`
2. **Preserve scheduling and failure semantics** — Continue constructing one `asyncio.Semaphore` per `CodeJobService` and using the existing acquisition boundaries for planning, coding, rebasing, workflow-reference recovery, and CI repair. Keep check polling outside a slot when it does not invoke Codex. Preserve `async with` release behavior on success, handled exceptions, and cancellation, along with current task scheduling, database status transitions, recovery, queue snapshots, and shutdown behavior.
   - Likely files: `src/telegram_project_manager/bots/code_manager/service.py`, `src/telegram_project_manager/platform/storage/db.py`
3. **Add deterministic concurrency coverage** — Add an asynchronous `CodeJobServiceTests` regression test that uses the default constructor and a blocking fake Codex adapter coordinated by `asyncio.Event`. Submit five distinct `skip_plan=True` issue jobs, wait with bounded polling until four Codex turns have entered, then assert the named default equals four, four jobs appear in running statuses, and the fifth remains `queued_code`. Release one blocked turn and verify the fifth enters Codex, proving queued work is promoted when a permit becomes available. Release all remaining turns and perform service shutdown in `finally` so failures do not leak tasks or temporary workspaces.
   - Likely files: `tests/test_code_manager.py`
4. **Confirm compatibility boundaries** — Keep application wiring unchanged so `run_bot` receives the new service default automatically. Verify `/queue` still classifies jobs only by existing persisted statuses and that recovery schedules every eligible persisted job while the semaphore limits active Codex phases to four. Do not alter independent limits for `/ask` (two), `/brainstorm` (one), or `/do` (two), and do not add a runtime configuration setting because the issue requests a fixed increase rather than configurability.
   - Likely files: `src/telegram_project_manager/main.py`, `tests/test_codex_queue.py`, `src/telegram_project_manager/bots/ask_manager/service.py`, `src/telegram_project_manager/bots/ideas/service.py`, `src/telegram_project_manager/bots/do_manager/service.py`
5. **Validate and roll out** — Run the focused concurrency test, the complete code-manager and queue test modules, then the full unittest suite. Deploy the code and restart the main Telegram bot process so the newly constructed semaphore has four permits; the separate `/do` worker needs no change. Prefer restarting when no `/code` phases are running because current shutdown/recovery semantics mark interrupted active code jobs for manual retry or discard, while persisted queued jobs are scheduled under the new limit after startup.
   - Likely files: `tests/test_code_manager.py`, `tests/test_codex_queue.py`, `src/telegram_project_manager/main.py`

## Validation

- `uv run python -m unittest tests.test_code_manager.CodeJobServiceTests.test_default_allows_four_concurrent_code_jobs` — verify four jobs enter concurrently, the fifth waits, and it starts after a permit is released.
- `uv run python -m unittest tests.test_code_manager` — validate existing planning, coding, CI repair, rebase, recovery, cancellation, and shutdown behavior.
- `uv run python -m unittest tests.test_codex_queue` — confirm running/queued status classification and the durable queued-job count remain unchanged.
- `uv run python -m unittest discover -s tests` — run the complete repository regression suite.

## Risks

- Four simultaneous `/code` phases can approximately double peak Codex provider requests, CPU, memory, disk I/O, worktree activity, and related GitHub operations compared with the current two-slot limit.
- The semaphore is process-local; if multiple main bot processes run against the same database, aggregate `/code` concurrency can exceed four.
- The ten-job cap counts queued statuses rather than running jobs, so the system can have up to four slot-consuming jobs plus ten queued jobs, in addition to jobs paused for approval, checks, retry, merge, or deployment.
- A concurrency regression test can become flaky if it relies on fixed sleeps; explicit events and bounded condition polling are required.
- Restarting to activate the new default interrupts active `/code` phases under existing behavior, so rollout should avoid an active-job window or account for manual `/code retry` or `/code discard` handling.
