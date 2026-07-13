# Telegram GitHub Commit Manager Bot

## Goal

Build a Telegram bot that can be added to a Telegram group and act as a GitHub commit manager. Users message the bot with natural language requests, the bot uses an OpenAI-compatible API to understand the request, then performs approved GitHub actions such as creating commits and posting commit comments. The bot replies in Telegram with the commit link, summary, changed files, and any follow-up status.

This project should be designed as the first bot in a larger Telegram project manager system. The commit manager is the current priority, but the code should not be written as a one-off bot. Shared pieces such as Telegram routing, permissions, OpenAI-compatible API access, storage, auditing, configuration, and response formatting should be reusable by future bots.

Expected future bots may include:

- Pull request manager bot.
- Oversight bot for checking project state, gathering information, and reporting status.
- Ideas and brainstorming bot.

The first implementation should still stay focused on the commit manager. Future bots should influence the architecture, not expand the first milestone.

Runtime constraints:

- Use Python.
- Use `uv` for dependency management, lockfiles, and running the app.
- Use Telegram Bot API long polling with only a bot token.
- Keep the application lean enough to run on a weak VPS.
- Avoid heavy frameworks unless they clearly remove more complexity than they add.

## Initial Scope

The first version should support:

- Receive messages from a Telegram group.
- Only respond when mentioned, replied to, or called with a command.
- Use an OpenAI-compatible API for intent parsing, planning, and response generation.
- Connect to GitHub using a configured credential.
- Let bot admins define the active GitHub repository from chat commands.
- Create commits in the admin-selected repository.
- Add a comment/status note for created commits.
- Return useful Telegram responses with commit URL, branch, commit SHA, summary, and changed files.
- Keep audit logs of who requested what and what GitHub action was taken.

Out of scope for the first version:

- Full pull request management.
- Multi-repository routing from arbitrary natural language.
- Autonomous commits without user confirmation.
- Long-running code generation across large codebases.
- Direct production deployment automation.

## High-Level Architecture

The architecture should separate shared platform code from bot-specific behavior. The commit manager should be implemented as one capability/module inside the project manager system, not as the whole system.

```text
Telegram Group
    |
    v
Telegram Bot Webhook / Polling
    |
    v
Command Router
    |
    +--> Auth / Permission Check
    |
    +--> OpenAI-Compatible LLM Client
    |       |
    |       v
    |   Intent + Commit Plan
    |
    +--> GitHub Service
    |       |
    |       +--> Read repo state
    |       +--> Create branch or use target branch
    |       +--> Create/update files
    |       +--> Create commit
    |       +--> Add commit comment
    |
    v
Telegram Response
```

Shared platform responsibilities:

- Telegram update receiving.
- Command and mention routing.
- User, chat, and role permissions.
- OpenAI-compatible API client.
- Prompt/schema execution helpers.
- Storage and migrations.
- Audit logs.
- Rate limits.
- Common Telegram response formatting.
- Configuration loading.

Commit manager responsibilities:

- Understand commit-related requests.
- Build commit plans.
- Validate file changes.
- Create GitHub commits.
- Add GitHub commit comments.
- Return commit-specific links and metadata.

## Core Behavior

Example user message:

```text
@CommitBot connect to GitHub and create a commit about adding setup instructions
```

Expected bot flow:

1. Detect bot mention.
2. Verify user is allowed to trigger GitHub actions.
3. Send message content to the LLM with repo context and strict tool schema.
4. LLM returns structured intent:

```json
{
  "action": "create_commit",
  "repo": "owner/repository",
  "branch": "main",
  "summary": "Add setup instructions",
  "files": [
    {
      "path": "SETUP.md",
      "operation": "create_or_update",
      "content_description": "Document local setup, environment variables, and run commands."
    }
  ],
  "needs_confirmation": true
}
```

5. Bot previews plan in Telegram and asks for confirmation.
6. User confirms with a command such as:

```text
/confirm abc123
```

7. Bot creates commit on GitHub.
8. Bot posts a commit comment on GitHub.
9. Bot replies with commit information.

Example final Telegram response:

```text
Commit created.

Repo: owner/repository
Branch: main
Commit: abc1234
Message: Add setup instructions
Files changed:
- SETUP.md

Link: https://github.com/owner/repository/commit/abc1234
Comment: https://github.com/owner/repository/commit/abc1234#commitcomment-...
```

## Telegram Interaction Model

Telegram connectivity should use Bot API long polling with a BotFather token. It should not require a Telegram API ID or API hash, public webhook URL, reverse proxy, or TLS certificate.

The bot should respond only when:

- Message starts with `/commit`.
- Message mentions the bot username.
- Message replies to a bot prompt.
- Message starts with an agreed prefix, for example `bot:`.

Suggested commands:

```text
/start
/help
/status
/commit <request>
/confirm <plan_id>
/cancel <plan_id>
/repos
/repo set owner/repository
/repo show
/repo clear
/branch <branch_name>
```

Group behavior:

- Ignore random group chatter.
- Require explicit mention or command.
- Include requesting Telegram user ID in audit logs.
- Restrict high-risk commands to allowlisted Telegram user IDs.

Repository selection behavior:

- Bot admins define the active repository from Telegram chat.
- Normal users cannot change the active repository.
- `/repo set owner/repository` sets the active repository for the current Telegram chat.
- `/repo show` shows the current active repository and default branch.
- `/repo clear` removes the chat-level active repository.
- Commit requests use the current chat's active repository unless an admin changes it.
- If no repository is configured for the chat, `/commit` should refuse to create a plan and ask an admin to run `/repo set owner/repository`.
- Repository values must still be checked against `GITHUB_ALLOWED_REPOS`.
- Repository configuration should be stored per Telegram chat, so different groups can point to different repos later.

## GitHub Integration

Recommended GitHub access for first version:

- Use the GitHub CLI (`gh`) as the GitHub tool layer.
- The bot should call `gh` commands from Python instead of implementing GitHub REST API calls directly for the first version.
- The VPS should have `gh` installed and authenticated before the bot runs.
- `gh auth status` should be checked by `/status`.
- The bot should still enforce its own allowlist, permissions, confirmation, and repo validation before running any `gh` command.

Authentication setup:

- Use `gh auth login` or `GH_TOKEN` outside the bot.
- The bot should not perform interactive GitHub login.
- The bot should not store the GitHub token in SQLite.
- SQLite stores repository config and permissions, not GitHub secrets.

GitHub command execution rules:

- Use non-interactive `gh` commands only.
- Use structured output with `--json` where available.
- Capture stdout, stderr, exit code, and duration for audit logs.
- Set command timeouts.
- Never pass unvalidated user text directly into shell commands.
- Prefer argument arrays over shell strings.
- Validate repo names, branch names, file paths, and commit messages before invoking `gh`.
- Run commands from a controlled working directory.
- If local git is needed for commit creation, use a dedicated workspace directory under `./data/workspaces/`.

Commit creation options:

### Option A: `gh api`

Use `gh api` to call GitHub endpoints for creating blobs, trees, commits, refs, and comments.

Pros:

- No direct HTTP client needed for GitHub.
- Uses existing `gh` authentication.
- No local clone needed for API-based commits.
- Good fit for weak VPS.

Cons:

- Still requires understanding GitHub Git Data API concepts.
- `gh api` command arguments must be built carefully.

### Option B: Local git workspace plus `gh`

Use `gh repo clone` or `git clone`, make file changes locally, commit with git, then push with `gh`/git auth.

Pros:

- Easier mental model.
- Can use normal git diff and commit behavior.
- Better if later bots need local repo inspection.

Cons:

- Uses more disk and network.
- Needs workspace cleanup.
- More stateful.
- Can be heavier on a weak VPS.

Recommended first implementation:

- Use `gh api` for multi-file commits.
- Create a new branch for each request by default:

```text
bot/<telegram-user>/<short-plan-id>
```

- Optionally allow direct commit to `main` only for admin users.

## OpenAI-Compatible API Integration

The bot should support any OpenAI-compatible API. Normal model configuration should live in SQLite and be managed through Telegram admin commands:

```text
/config set openai_base_url https://api.openai.com/v1
/config set openai_model gpt-5-mini
/config set llm_memory_max_messages 12
```

Only the API key should be treated as a secret and loaded from process environment or `./data/secrets.json`:

```env
OPENAI_API_KEY=...
```

LLM usage:

- Intent classification.
- Commit plan generation.
- Commit message generation.
- GitHub comment generation.
- Telegram response formatting.

The LLM should not directly execute actions. It should return structured JSON. Local code validates the JSON and performs GitHub actions.

Use LangChain `BaseChatMessageHistory` and message types with a bounded SQLite-backed history per Telegram chat. Admins should be able to inspect the message count and clear a chat's memory.

Suggested response schema:

```json
{
  "intent": "create_commit",
  "confidence": 0.0,
  "repo": "owner/name",
  "base_branch": "main",
  "target_branch": "bot/request-id",
  "commit_message": "Short imperative commit message",
  "changes": [
    {
      "path": "README.md",
      "operation": "create_or_update",
      "content": "Full proposed file content"
    }
  ],
  "github_comment": "Short comment explaining why this commit was created.",
  "requires_confirmation": true,
  "questions": []
}
```

Validation rules:

- Reject invalid JSON.
- Reject missing repo/branch/path/content.
- Reject path traversal such as `../`.
- Reject secrets or credential-looking content.
- Enforce max file count and max content size.
- Require confirmation before writing to GitHub.

## Safety Model

Required safety gates:

- Telegram user allowlist.
- Repository allowlist.
- Branch protection behavior.
- Confirmation before write actions.
- Dry-run preview before commit.
- Audit log for every request and result.
- Rate limit per user.
- Max files per commit.
- Max bytes per commit.
- Refuse changes to sensitive paths unless explicitly allowed.

Sensitive paths to protect by default:

```text
.env
.env.*
*.pem
*.key
id_rsa
id_ed25519
.github/workflows/*
```

Recommended default:

- Bot creates commits on bot branches.
- Bot does not push to `main` directly.
- Human reviews branch or PR later.

## Data Storage

The bot needs small persistent storage:

- Pending plans.
- Confirmation tokens.
- Telegram user permissions.
- Repo defaults and chat-level active repository.
- Audit events.

Simple first version:

- SQLite database.

Tables:

```text
users
- telegram_user_id
- username
- role
- created_at

plans
- id
- telegram_chat_id
- telegram_user_id
- repo
- base_branch
- target_branch
- request_text
- plan_json
- status
- created_at
- expires_at

audit_events
- id
- plan_id
- action
- status
- details_json
- created_at

chat_settings
- telegram_chat_id
- active_repo
- default_branch
- updated_by_user_id
- updated_at
```

## Configuration

Configuration should be stored in SQLite and managed from Telegram admin commands where possible. Avoid relying on a `.env` file for normal bot configuration.

SQLite-backed settings should include:

- Allowed Telegram chats.
- Admin Telegram user IDs.
- Active repository per chat.
- Allowed GitHub repositories.
- Default branch per chat or repo.
- Direct commit permissions.
- Confirmation requirement.
- Max files per commit.
- Max bytes per commit.
- OpenAI-compatible base URL.
- OpenAI model.

Admin commands should manage these settings over time:

```text
/config show
/config set openai_base_url <url>
/config set openai_model <model>
/config set max_files_per_commit <number>
/config set max_bytes_per_commit <number>
/config set require_confirmation true|false
/admin add <telegram_user_id>
/admin remove <telegram_user_id>
/repo allow owner/repository
/repo disallow owner/repository
```

Only secrets should come from process environment or a local secret file outside git:

```env
TELEGRAM_BOT_TOKEN=
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
```

The database path should use a simple default such as `./data/bot.db`. It can be overridden by a CLI flag later if needed, but it should not require a `.env` file.

Dependency and runtime notes:

- Use `uv` as the only Python package manager.
- Commit `pyproject.toml` and `uv.lock` once code implementation starts.
- Prefer direct `uv run ...` commands for local execution and deployment scripts.
- Keep dependency count small.
- Avoid background workers, task queues, Redis, Celery, or web frameworks in the first version.
- SQLite should be enough for the first version.

## Suggested Project Structure

```text
src/
  platform/
    telegram.py
    permissions.py
    router.py
    responses.py
    config.py
  platform/llm/
    client.py
    schemas.py
  platform/storage/
    db.py
    models.py
    audit.py
  integrations/gh/
    runner.py
    commits.py
    comments.py
  bots/
    commit_manager/
      commands.py
      prompts.py
      schemas.py
      planner.py
      executor.py
    pull_request_manager/
      README.md
    oversight/
      README.md
    ideas/
      README.md
  main.py
tests/
  test_permissions.py
  test_planner_schema.py
  test_github_commit_builder.py
  test_commands.py
```

The placeholder bot folders are only markers for future growth. The first working code should be for `bots/commit_manager/`.

## Commit Flow Details

Recommended GitHub commit algorithm using `gh api`:

1. Get base branch ref:

```text
gh api repos/{owner}/{repo}/git/ref/heads/{base_branch}
```

2. Get base commit.
3. Get base tree SHA.
4. Create blobs for each file content.
5. Create new tree based on base tree.
6. Create new commit with parent base commit.
7. Create or update target branch ref.
8. Create commit comment:

```text
gh api repos/{owner}/{repo}/commits/{commit_sha}/comments
```

9. Reply to Telegram with commit URL.

## LLM Prompt Requirements

System prompt should force structured output:

```text
You are a GitHub commit planning assistant.
Return only valid JSON matching the provided schema.
Do not execute actions.
Do not include secrets.
Ask clarifying questions when the request is ambiguous.
Prefer small, reviewable commits.
```

The app should provide context:

- Active repo selected for the Telegram chat.
- Allowed repos.
- Default branch.
- Current Telegram user role.
- Max files and bytes.
- Existing repo file list, if available.

## Error Handling

Bot should produce clear Telegram errors:

- Unauthorized user.
- Unsupported command.
- Missing active repository for the Telegram chat.
- Ambiguous request.
- LLM invalid output.
- GitHub auth failure.
- Branch conflict.
- File too large.
- Rate limit hit.
- Commit failed.

Example:

```text
Commit not created.

Reason: request needs confirmation.
Plan ID: abc123
Run: /confirm abc123
```

## Testing Plan

Unit tests:

- Telegram command parsing.
- Mention detection.
- Permission checks.
- LLM schema validation.
- GitHub path validation.
- Commit message validation.

Integration tests:

- Mock Telegram update to pending plan.
- Mock confirmation to GitHub commit call.
- Mock GitHub failure to Telegram error.

Manual tests:

- Add bot to test group.
- Send `/status`.
- Send `/commit add README note`.
- Confirm plan.
- Verify branch and commit on GitHub.
- Verify commit comment.

## Implementation Phases

### Phase 1: Local Bot Skeleton

- Telegram bot receives commands.
- User allowlist works.
- `/status` and `/help` work.
- SQLite storage initialized.

### Phase 2: LLM Planner

- OpenAI-compatible client added.
- `/commit <request>` creates structured pending plan.
- Plan preview shown in Telegram.
- `/confirm` and `/cancel` work locally.

### Phase 3: GitHub Commit Execution

- GitHub client added.
- Bot creates branch and commit.
- Bot posts commit comment.
- Telegram response includes commit link and SHA.

### Phase 4: Hardening

- Add rate limits.
- Add sensitive path blocks.
- Add better audit logs.
- Add tests.
- Add Docker deployment.

### Phase 5: Better GitHub Workflow

- Optional pull request creation.
- Optional diff preview.
- Repo file context retrieval.
- Multiple repo support.
- GitHub App authentication.

## Open Questions

- Should the bot commit directly to `main`, or always create a bot branch?
- Should the bot generate file contents itself, or only commit files that already exist locally?
- Should it support pull requests in the first version?
- Which OpenAI-compatible provider/model will be used?
- Should GitHub auth be a PAT first, or GitHub App from the start?
- Should the Telegram group allow all members to request commits, or only admins?
- Should each Telegram user map to a GitHub identity in commit metadata?

## Recommended Defaults

- Python implementation.
- `uv` for Python dependency management and execution.
- Telegram Bot API long polling for connectivity.
- `langchain-openai` and `ChatOpenAI` for OpenAI-compatible API calls.
- `gh` CLI for GitHub operations.
- `pydantic` for LLM output validation.
- SQLite for local persistence.
- `gh api` with GitHub Git Data API endpoints for commits.
- Confirmation required for every GitHub write.
- Bot branches by default, direct `main` commits disabled.

## Lean VPS Requirements

The bot should be designed for low memory and low CPU usage:

- Single Python process.
- No always-on browser automation.
- No local repo clone unless a later feature requires it.
- Prefer `gh api` over cloning repositories.
- No container requirement for the first version.
- No queue system for the first version.
- Minimal dependencies.
- Simple logging to stdout and rotating file logs if needed.
- SQLite database stored on disk.
- Polling-based Telegram connection using only a BotFather token.

The first version should favor simple, predictable operations over complex infrastructure. GitHub API calls and LLM calls should happen only when a valid Telegram command requires them.
