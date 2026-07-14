# Telegram Project Manager

Lean Telegram bot platform for project-management bots. The first bot is a GitHub commit manager controlled from Telegram.

## Runtime

- Python 3.11+
- `uv`
- LangChain `ChatOpenAI`
- OpenAI Codex SDK for `/code` planning and implementation
- GitHub CLI (`gh`) installed and authenticated
- SQLite database at `./data/bot.db` by default

## Setup

```powershell
uv sync
uv run telegram-project-manager init-db
uv run telegram-project-manager admin add <telegram_user_id>
uv run telegram-project-manager run
```

`./data/secrets.json` contains only the Telegram bot token:

```json
{
  "TELEGRAM_BOT_TOKEN": "..."
}
```

Telegram API ID and API hash are not used. After startup, message the bot privately as an admin:

```text
/config set openai_api_key <key>
/config set openai_base_url https://api.openai.com/v1
/config set openai_model <model>
```

The API key is stored separately in SQLite and never returned by `/config show`. Setting it in group chats is blocked.
The `/code` workflow has separate `codex_api_key`, `codex_base_url`, and
`codex_model` settings. They are passed to the Codex SDK runtime as
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, and the thread model, so Codex can use a
different provider without changing issue or commit generation.

GitHub auth is handled by `gh`, not by the bot:

```powershell
gh auth login
gh auth status
```

## Telegram Commands

Commands marked `admin` require a registered Telegram admin.

### General

```text
/start                         Show help
/help                          Show help
help                           Show help without a slash
/status                        Show service configuration and GitHub auth status
```

### Repository and branch

Each chat or group has one active repository and default branch. The repository
allowlist is global.

```text
/repos                          List allowed repositories
/repo show                      Show this chat's repository and branch
/repo allow owner/repository    Allow a repository (admin)
/repo disallow owner/repository Disallow a repository (admin)
/repo set owner/repository      Set this chat's repository (admin)
/repo clear                     Clear this chat's repository (admin)
/repo local set <absolute-path> Set this chat's service-owned Git cache (admin)
/repo local clear               Clear this chat's Git cache setting (admin)
/branch <branch_name>           Set this chat's default branch (admin)
```

### Commit workflow

Generated plans include current actual behavior and expected behavior after the change.

```text
/commit <request>              Generate a commit plan (admin)
/confirm <plan_id>             Execute a commit plan (admin)
/cancel <plan_id>              Cancel a plan (requester or admin)
```

### Issue workflow

Issue drafts use the active repository and local Git cache for the current chat.
Before drafting, the bot fetches branch deltas and reads a bounded, read-only
snapshot of project documentation and relevant source files at the fetched
commit. The issue-drafting LLM improves the
prompt into a title, summary, actual behavior, expected behavior, codebase
context, relevant files, and evidence-backed possible causes. Context retrieval
must succeed before a draft is created. Images are embedded from an isolated
issue-assets branch and are not sent to the issue-drafting LLM. When `/code`
runs on the created issue, those managed images are validated, staged
temporarily, and supplied to Codex as vision inputs.

Issue body generation is enabled by default. Set
`/config set issue_body_llm_enabled false` to skip repository analysis and use
the original Telegram prompt verbatim as the GitHub issue body. In this mode the
LLM generates only the title; text feedback retitles the draft without changing
its body. The mode is pinned when each draft is created.

```text
/issue <prompt>                Generate an issue draft (admin)
/edit <i-draft_id> <feedback>  Revise a pending issue draft (original author)
/confirm <i-draft_id>          Create the reviewed GitHub issue (requesting admin)
/cancel <i-draft_id>           Cancel an issue draft (admin)
```

For images, send one photo or a captioned Telegram album with /issue as its
caption. JPEG, PNG, and GIF are supported, up to 10 images, 10 MB each, and
20 MB total. Before confirmation, the original author can reply directly to any
draft preview with feedback, images, or both. Text feedback regenerates the
draft using fresh repository context; new images are appended. Each successful
edit keeps the same draft ID, records a revision, and renews the one-hour expiry.

### Code workflow

`/code` operates on an existing, open GitHub issue in an allowed repository with
a configured local Git cache. The bot fetches only new Git objects, creates an
isolated linked worktree for the job branch, and lets Codex inspect it in a
read-only sandbox before publishing its structured plan as the first commit and
body of a draft pull request. The Telegram progress message updates as Codex
changes phases, runs commands, and touches files; raw model reasoning is never
shown.

Reply `approve` to the progress message to implement the plan. Reply with any
other text to revise the plan on the same draft pull request, or reply `discard`
to close the pull request and delete its branch. After approval, Codex works in
a workspace-write sandbox, must report a successful validation command, removes
  the temporary plan file, pushes the implementation, and marks the pull request
  ready for review. The job then waits for every reported check on the current PR
  head. Passed, neutral, and skipped checks are accepted; ordinary CI failures are
  sent back to Codex for up to two focused repair commits. Cancelled, timed-out,
  stale, action-required, and startup failures are reported for manual action.
  Each pushed commit may wait up to 30 minutes, while repositories that publish no
  checks remain supported after a 60-second discovery grace. The Telegram job is
  reported as ready and its workspace is cleaned only after this gate succeeds.
  A separate short Telegram alert is sent when the job becomes ready or fails.
Job, draft, and plan messages include native controls. IDs remain copyable,
valid commands run when tapped, and GitHub buttons open their target directly.
Commands that need typed input, such as edit feedback, remain copyable. Deploy
always asks for confirmation before merging.

```text
/code #123                         Plan an issue in this chat's active repo
/code owner/repository#123         Plan an issue in another allowed repo
/code <GitHub issue URL>           Plan from a full issue URL
/code #123 --skip-plan             Implement immediately, then open a PR
/code approve <c-job_id>           Approve a published plan
/code edit <c-job_id> <feedback>   Revise the plan on the same draft PR
/code discard <c-job_id>           Close the draft PR and delete its branch
/code retry <c-job_id>             Retry a failed or interrupted phase
/code rebase <c-job_id>            Rebase a ready PR onto main and rerun CI
/code status [c-job_id]            Show one job or recent jobs in this chat
/deploy c-job_id                   Squash-merge a ready PR and watch deployment
/deploy                            Same action when replying to its code-job message
```

If a ready pull request received a new commit after its recorded CI pass,
`/code rebase` first adopts that head and checks it without modifying the
branch. When the job returns to `ready`, run the rebase command again.

You can also reply `/code` to the bot's `Issue created` message. Plan controls
may be sent by the requester or any registered admin. At most two Codex jobs run
concurrently and ten may wait in the queue. Service restarts mark active turns
as interrupted so an admin can explicitly retry or discard them.

The bot blocks changes to `.env*`, private-key files, and
`.github/workflows/*`, and rejects more than 100 changed files or 5 MB of
changes. GitHub CLI credentials must be configured for the service account with
permission to fetch, push branches, and manage pull requests.

### Merge and deployment workflow

Configure a GitHub Actions workflow with a `workflow_dispatch` trigger and a
required `ref` input once for each allowed repository. The value may be a
workflow name or workflow file:

```text
/repo deploy set owner/repository deploy.yml
/repo deploy clear owner/repository
```

After a `/code` job reports `ready`, an admin in the originating chat may tap
**Deploy** and confirm, run `/deploy c-job_id`, or reply `/deploy` to the job
message. The bot requires a PR targeting `main`, verifies that its head is the
exact SHA accepted by the CI gate, honors reviews, branch protection, and merge
queues, then squash-merges and deletes the feature branch. The squash commit
uses an explicit issue-closing message so GitHub does not add the automation
account as a co-author. The bot dispatches the configured workflow at the merge
SHA, waits up to two minutes for it to appear, and allows 30 minutes to finish.
The bot reports merge and deployment failures separately and resumes an active
monitor after restart.

### Local repository cache

The configured cache must be an absolute path to a normal or bare Git repository
that is readable and writable by the bot service account. Its literal `origin`
URL must identify the chat's active `owner/repository`. Missing, inaccessible,
or mismatched caches fail clearly; issue and code workflows never fall back to a
fresh clone.

Seed the managed cache once from an existing root-owned checkout without
downloading the repository again:

```bash
sudo install -d -o telegram-pm -g telegram-pm /var/lib/telegram-project-manager/repos
sudo git clone --mirror --no-hardlinks /root/trade-router \
  /var/lib/telegram-project-manager/repos/mehrangta--telegram-trade-router.git
sudo git -C /var/lib/telegram-project-manager/repos/mehrangta--telegram-trade-router.git \
  config remote.origin.mirror false
sudo git -C /var/lib/telegram-project-manager/repos/mehrangta--telegram-trade-router.git \
  config remote.origin.url https://github.com/mehrangta/telegram-trade-router.git
sudo git -C /var/lib/telegram-project-manager/repos/mehrangta--telegram-trade-router.git \
  config --replace-all remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
sudo chown -R telegram-pm:telegram-pm /var/lib/telegram-project-manager/repos
```

Then run in the Telegram chat:

```text
/repo local set /var/lib/telegram-project-manager/repos/mehrangta--telegram-trade-router.git
```

### Configuration

```text
/config show                                Show redacted configuration (admin)
/config set openai_api_key <key>            Set API key (admin, private chat only)
/config set openai_base_url <url>           Set OpenAI-compatible URL (admin)
/config set openai_model <model>            Set model (admin)
/config set codex_api_key <key>             Set Codex API key (admin, private chat only)
/config set codex_base_url <url>            Set Codex provider URL (admin)
/config set codex_model <model>             Set Codex model (admin)
/config set issue_body_llm_enabled <value>  Enable/disable LLM issue bodies (admin)
/config set llm_memory_max_messages <count> Set even memory limit, minimum 2 (admin)
/config set max_files_per_commit <count>    Set file limit (admin)
/config set max_bytes_per_commit <count>    Set byte limit (admin)
/config set require_confirmation <value>    Set confirmation policy (admin)
```

### Memory and admins

Memory is isolated per chat or group.

```text
/memory status                   Show memory usage
/memory show                     Alias for /memory status
/memory clear                    Clear this chat's memory (admin)
/admin add <telegram_user_id>    Add an admin (admin)
/admin remove <telegram_user_id> Remove an admin (admin)
```

## Command-Line Interface

```text
telegram-project-manager [--db <path>] init-db
telegram-project-manager [--db <path>] run
telegram-project-manager [--db <path>] admin add <telegram_user_id> [--username <username>]
telegram-project-manager [--db <path>] admin remove <telegram_user_id>
telegram-project-manager [--db <path>] config show
telegram-project-manager [--db <path>] config set <key> <value>
```

CLI configuration keys: `openai_api_key`, `openai_base_url`, `openai_model`,
`llm_memory_max_messages`, `max_files_per_commit`, `max_bytes_per_commit`,
and `require_confirmation`.

## VPS Service Commands

```bash
sudo systemctl enable --now telegram-project-manager
sudo systemctl restart telegram-project-manager
sudo systemctl stop telegram-project-manager
sudo systemctl status telegram-project-manager
sudo journalctl -u telegram-project-manager -f
sudo -u telegram-pm -H gh auth login --hostname github.com --web
sudo -u telegram-pm -H gh auth status --hostname github.com
```

OpenAI credentials and provider configuration are stored in SQLite and managed through admin `/config set` commands. Only the Telegram bot token lives in `data/secrets.json`.
LLM requests use LangChain's `langchain-openai` integration with JSON Schema structured output.
LangChain message history is stored in SQLite per Telegram chat. The latest 12 messages are retained by default; admins can change the limit or clear the current chat's memory.
