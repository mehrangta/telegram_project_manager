# Telegram Project Manager

Telegram bot for managing GitHub issues, repository-aware Codex jobs, pull
requests, merges, and deployments from Telegram.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- GitHub CLI (`gh`) authenticated for the service account
- OpenAI-compatible API access
- SQLite (`data/bot.db` by default)
- A local or bare Git cache for each managed repository

## Quick start

Install the project and initialize its database:

```bash
uv sync
uv run telegram-project-manager init-db
uv run telegram-project-manager admin add <telegram_user_id>
```

Create `data/secrets.json` with the Telegram token:

```json
{"TELEGRAM_BOT_TOKEN":"..."}
```

Authenticate the service account with GitHub, then start the bot:

```bash
gh auth login
gh auth status
uv run telegram-project-manager run
```

In a private admin chat, configure the providers and models:

```text
/config set openai_api_key <key>
/config set openai_model <model>
/config set codex_api_key <key>
/config set codex_plan_model <model>
/config set codex_code_model <model>
```

Set `openai_base_url` or `codex_base_url` only for compatible custom endpoints.
`codex_model` remains a shared fallback when a phase-specific model is unset.

## Common commands

Commands that change configuration or start work require a registered admin.
Use `/help` in Telegram for the complete command reference.

```text
/status                                      Show service and GitHub status
/repo setup owner/repository                Configure and cache a repository
/repo show                                  Show current repository settings
/issues                                     List open issues
/issue <prompt>                             Draft a GitHub issue
/commit <request>                           Draft a direct commit plan
/code #123                                  Plan an issue in the active repository
/code approve <c-job_id>                    Approve implementation
/code status <c-job_id>                     Show code-job status
/ask <question> [images]                    Inspect the active repository
/do <job> [images]                          Run a writable repository job
/do status [d-job_id]                       Show do-job status
/merge <c-job_id>                           Merge without deployment
/deploy <c-job_id>                          Merge and deploy
/config show                                Show redacted configuration
/memory status                              Show chat or topic memory usage
```

`/code` also accepts `owner/repository#123`, an issue URL, and `--skip-plan`.
Use `/code edit`, `/code retry`, `/code rebase`, or `/code discard` to manage an
existing job. `/do --host <job>` is restricted to private admin chats.

## Workflows

- **Repository context:** Run `/repo setup owner/repository` to allow the
  repository, detect its default branch, create or refresh its managed cache,
  and select it for the current chat or forum topic. Topic settings are
  independent from group-level settings.
- **Issues and code:** `/issue` creates a reviewable issue draft. `/code` plans
  an existing issue in an isolated worktree and opens a draft pull request.
  Approve the plan before implementation unless `--skip-plan` was used. A job
  becomes ready only after repository validation and GitHub checks pass.
- **Questions and jobs:** `/ask` performs a read-only repository inspection.
  `/do` runs writable Codex work in a persistent repository workspace; its
  separate worker keeps queued jobs independent from Telegram polling.
- **Images:** `/issue`, `/ask`, and `/do` accept JPEG, PNG, and GIF attachments,
  with up to 10 images, 10 MB each, and 20 MB total.
- **Merge and deployment:** `/merge` squash-merges a ready pull request without
  deploying. Deployment is disabled per repository until an admin configures a
  workflow and enables it. `/deploy` requires a ready pull request targeting
  `main` and dispatches the configured `workflow_dispatch` workflow at the
  accepted merge SHA.
- **Recovery:** Interrupted code jobs require `/code retry` or `/code discard`.
  Interrupted `/do` jobs are not rerun automatically because they may have
  produced partial or non-idempotent changes.

## Safety

> **Warning:** `/ask`, `/code`, and `/do` run Codex with full host filesystem
> access and unrestricted outbound network access. Prompt restrictions are not
> sandbox-enforced. Use only trusted repositories and monitor the environment.

- Repositories must be explicitly allowed and are isolated by chat or topic.
- Code jobs use isolated Git worktrees and enforce a 20 MB change-size limit.
- API keys are stored separately in SQLite and redacted from bot output.
- Codex is instructed not to modify `.env` files, private keys, or
  `.github/workflows`, but these restrictions are prompt-level only.
- Commits, pushes, pull requests, merges, and deployments are performed by the
  host application after its checks and confirmations.

See [Agent approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security#run-codex-in-dev-containers)
for guidance on running Codex with full access.

## Advanced operation

For manual repository setup, first allow and select the repository, then provide
an absolute path to a writable normal or bare Git repository:

```text
/repo allow owner/repository
/repo set owner/repository
/repo local set <absolute-path>
/repo check
```

The cache's literal `origin` must match `owner/repository`. Managed caches created
by `/repo setup` live under the database directory's `repos` folder.

Durable `/do` jobs require a separate worker:

```bash
uv run telegram-project-manager run-do-worker
```

A systemd unit template is available at
`deploy/telegram-project-manager-do-worker.service`.

Use the CLI help for all service commands and configuration keys:

```bash
uv run telegram-project-manager --help
uv run telegram-project-manager config set --help
```
