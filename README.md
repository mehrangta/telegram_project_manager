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

## Command reference

### CLI commands

Use `telegram-project-manager` as the canonical executable. The installed `tpm`
alias and `python -m telegram_project_manager` run the same CLI. The global
`--db <path>` option must precede the subcommand and defaults to `data/bot.db`.

| Command | Description |
| --- | --- |
| `telegram-project-manager [--db <path>] init-db` | Initialize or upgrade the SQLite database. |
| `telegram-project-manager [--db <path>] run` | Start the Telegram polling service. |
| `telegram-project-manager [--db <path>] run-do-worker` | Start the durable full-access `/do` and `/goal` worker. |
| `telegram-project-manager [--db <path>] admin add <telegram_user_id> [--username <username>]` | Register a Telegram administrator. |
| `telegram-project-manager [--db <path>] admin remove <telegram_user_id>` | Remove a registered administrator. |
| `telegram-project-manager [--db <path>] config show` | Show settings with secret values redacted. |
| `telegram-project-manager [--db <path>] config set <key> <value>` | Store a supported setting or secret. |

Supported configuration keys:

| Key | Value and purpose |
| --- | --- |
| `openai_api_key` | Secret used by the OpenAI-compatible planning client. |
| `openai_base_url` | Absolute HTTP or HTTPS endpoint for the planning client. |
| `openai_model` | Model used for issue and direct-commit planning. |
| `codex_api_key` | Secret used by the Codex SDK. |
| `codex_base_url` | Absolute HTTP or HTTPS Codex-compatible endpoint. |
| `codex_model` | Shared fallback when a phase-specific Codex model is unset. |
| `codex_plan_model` | Model used for code-job planning. |
| `codex_code_model` | Model used for implementation and repository jobs. |
| `max_files_per_commit` | Maximum files allowed in a direct-commit plan; defaults to `10`. |
| `max_bytes_per_commit` | Maximum bytes allowed in a direct-commit plan; defaults to `100000`. |
| `require_confirmation` | Accepted compatibility setting for confirmation behavior. |
| `issue_body_llm_enabled` | `true` or `false`; controls generated issue bodies. |
| `llm_memory_max_messages` | Even integer of at least `2`; limits stored LLM messages. |

### Telegram commands

Only registered administrators are routed to the bot. Commands are scoped to
the current chat or exact forum topic where applicable. Telegram-qualified
forms such as `/status@BotName` are accepted. `/help` is a compact in-bot quick
reference; the complete command surface is documented here.

#### General and repository commands

| Command | Description |
| --- | --- |
| `/start` | Show the in-bot command summary. |
| `/help` | Show the in-bot command summary. `help` also works in a private chat. |
| `/status` | Show GitHub authentication, repository, administrator, and model status. |
| `/repos` | List repositories in the allow list. |
| `/repo` or `/repo show` | Show repository, branch, cache, deployment, and brainstorming settings for the current scope. |
| `/repo check` | Show repository settings and validate the configured local cache. |
| `/repo allow owner/repository` | Add a repository to the allow list. |
| `/repo disallow owner/repository` | Remove a repository from the allow list. |
| `/repo set owner/repository` | Select an allowed repository for the current chat or topic. |
| `/repo setup owner/repository` | Allow the repository, detect its default branch, create or refresh its managed cache, and select it. |
| `/repo clear` | Clear the active repository for the current chat or topic. |
| `/repo local set <absolute-path>` | Set and validate a writable normal or bare Git cache for the active repository. |
| `/repo local clear` | Clear the local repository cache for the current chat or topic. |
| `/branch <branch_name>` | Set the default branch for the current chat or topic. |

#### Deployment and brainstorming configuration

| Command | Description |
| --- | --- |
| `/repo deploy enable owner/repository` | Enable deployment for an allowed repository. |
| `/repo deploy disable owner/repository` | Disable deployment for an allowed repository. |
| `/repo deploy set owner/repository <workflow-name-or-file>` | Set the `workflow_dispatch` workflow used for deployment. |
| `/repo deploy clear owner/repository` | Clear the configured deployment workflow. |
| `/repo brainstorm show owner/repository` | Show brainstorming state, schedule, destination, and last run. |
| `/repo brainstorm schedule owner/repository <daily\|weekly\|Nd> <HH:MM>` | Save a UTC cadence such as `daily`, `weekly`, or `2d`. |
| `/repo brainstorm enable owner/repository` | Enable manual and scheduled brainstorming using the current scope as its destination. |
| `/repo brainstorm disable owner/repository` | Disable brainstorming while preserving its schedule and destination. |

Enabling brainstorming requires the same repository to be active in the
current scope and requires a valid local cache. Deployment commands require an
allowed repository; deployment remains unavailable until both a workflow is
configured and deployment is enabled.

#### Issues and direct commits

| Command | Description |
| --- | --- |
| `/issues` | List open issues and their latest Codex code-job status for the active repository; ready jobs link to their tracked Telegram message when supported, and the list refreshes periodically. |
| `/issue <prompt> [images]` | Create a reviewable issue draft for the active repository. |
| `/edit i-12345678 [feedback] [images]` | Revise a pending issue draft; replying to its preview with text or images also revises it. |
| `/confirm i-12345678` | Create the GitHub issue from a pending issue draft. |
| `/cancel i-12345678` | Cancel a pending issue draft. |
| `/commit <request>` | Create a direct-commit plan for the active repository. |
| `/confirm <plan_id>` | Execute a pending direct-commit plan. |
| `/cancel <plan_id>` | Cancel a pending direct-commit plan. |

Issue draft IDs start with `i-`. Direct-commit plan IDs use the ID returned by
`/commit`; `/confirm` and `/cancel` route to the matching workflow. Drafts and
plans can be controlled only from their originating chat or topic.

#### Codex questions, ideas, and writable jobs

| Command | Description |
| --- | --- |
| `/ask <question> [images]` | Run a read-only Codex inspection of the active repository. |
| `/brainstorm` | Generate three ranked ideas for the enabled active repository. |
| `/queue` | Show running and queued `/code`, `/ask`, `/brainstorm`, and `/do` work for the current scope. |
| `/do <job> [images]` | Queue writable Codex work in the active repository's persistent workspace. |
| `/do --host <job> [images]` | Queue a host-wide writable job; available only in a private admin chat. |
| `/do status [d-12345678]` | Show one `/do` job or recent jobs in the current scope. |
| `/goal` or `/goal view` | Show the persistent goal for the current chat or topic. |
| `/goal set <description>` | Set one persistent repository goal for the current chat or topic and begin work. |
| `/goal edit <updated goal>` | Replace the objective; an active turn restarts with the new revision. |
| `/goal pause` | Pause future work and interrupt an active goal turn. |
| `/goal resume` | Resume a paused or blocked goal. |
| `/goal clear` | Stop and remove the current goal. |

#### Code jobs, pull requests, merges, and deployments

| Command | Description |
| --- | --- |
| `/code [--skip-plan] <issue-reference>` | Start a code job, with planning unless `--skip-plan` is present. |
| `/code approve c-12345678` | Approve an awaiting code-job plan. |
| `/code edit c-12345678 <feedback>` | Revise an awaiting code-job plan. |
| `/code discard c-12345678` | Discard a code job and its workspace. |
| `/code retry c-12345678` | Resume an interrupted or retryable code job. |
| `/code rebase c-12345678` | Rebase the code job onto its latest base branch. |
| `/code status [c-12345678]` | Show one code job or recent jobs in the current scope. |
| `/prs` | List open pull requests for the active repository. |
| `/merge c-12345678` | Squash-merge a ready code-job pull request without deploying. |
| `/deploy c-12345678` | Squash-merge a ready code-job pull request and start deployment. |

An issue reference may be `#123`, `123`, `owner/repository#123`, or a full
GitHub issue URL. Replying `/code` to an Issue created message uses that issue;
`--skip-plan` may still be included. When replying to a code-job message, the
plain text `approve`, `discard`, or `retry` runs that action, while other plain
text is treated as plan feedback. `/merge` and `/deploy` may also be sent as
replies without an explicit job ID.

#### Bot configuration and memory

| Command | Description |
| --- | --- |
| `/config` or `/config show` | Show configuration with API keys redacted. |
| `/config set <key> <value>` | Set any supported configuration key listed in the CLI section. API keys require a private chat. |
| `/memory`, `/memory status`, or `/memory show` | Show direct-commit and issue-planning memory use for the current scope. |
| `/memory clear` | Clear direct-commit and issue-planning memory for the current scope. |
| `/admin add <telegram_user_id>` | Register another administrator from Telegram. |
| `/admin remove <telegram_user_id>` | Remove an administrator from Telegram. |

## Workflows

- **Repository context:** Run `/repo setup owner/repository` to allow the
  repository, detect its default branch, create or refresh its managed cache,
  and select it for the current chat or forum topic. Topic settings are
  independent from group-level settings.
- **Issues and code:** `/issues` shows the latest local Codex code-job status for
  each listed issue when one exists and refreshes its tracked message
  periodically. Ready statuses link to the code job's tracked Telegram message
  in channels and supergroups; unsupported chats retain the plain status text.
  `/issue` creates a reviewable issue draft. After creation, choose Plan for the
  plan-and-approval workflow or Code to skip directly to implementation. The
  tracked code-job message replies to the original `/issue` request; jobs started
  directly with `/code` reply to that command instead. `/code` works on an
  existing issue in an isolated worktree and opens a draft pull request. Approve
  the plan before implementation unless `--skip-plan` was used. A job becomes
  ready only after repository validation and GitHub checks pass; its ready
  notification replies to the tracked code-job message.
- **Questions and jobs:** `/ask` performs a read-only repository inspection.
  `/brainstorm` returns three ranked ideas for new capabilities or meaningful
  extensions when brainstorming is enabled for the current chat or topic's
  active repository; it is not a bug finding or maintenance review. Each result
  uses a bounded single-message format with each idea in its own copyable block,
  validates that each prose field is a complete sentence, and makes one automatic
  regeneration attempt when model output is incomplete. Scheduled runs continue
  to use the destination saved for their repository. Configure one scheduled
  destination per repo with
  `/repo brainstorm schedule owner/repository daily 09:00` and
  `/repo brainstorm enable owner/repository`; schedule times are UTC, and
  disabling preserves the cadence and destination.
  `/do` runs writable Codex work in a persistent repository workspace. `/goal`
  pins the current repository and keeps making bounded progress toward one
  durable objective per exact chat or forum topic until it completes, is
  blocked, is paused, or is cleared. Goal edits are revisioned, and failed or
  interrupted turns block instead of replaying potentially non-idempotent work.
  The separate full-access worker keeps both workflows independent from
  Telegram polling and serializes writes to the same repository workspace.
- **Codex queue:** `/queue` shows `/code`, `/ask`, `/brainstorm`, `/goal`, and `/do` work currently
  running or queued for the current chat or exact forum topic. It excludes code
  jobs paused for input, approval, CI, retry, merge, deployment, or completion.
  Counts are scoped rather than global; `/ask` and `/brainstorm` entries are in
  memory and vanish after a bot restart, while `/do` and `/goal` state is durable.
- **Images:** `/issue`, `/edit`, `/ask`, and `/do` accept JPEG, PNG, and GIF
  attachments, with up to 10 images, 10 MB each, and 20 MB total.
- **Merge and deployment:** `/merge` squash-merges a ready pull request without
  deploying. Deployment is disabled per repository until an admin configures a
  workflow and enables it. `/deploy` requires a ready pull request targeting
  `main` and dispatches the configured `workflow_dispatch` workflow at the
  accepted merge SHA. If GitHub reports conflicts, the bot merges the latest
  base into the pull-request branch, asks Codex to resolve guarded content
  conflicts, reruns CI, and then resumes the requested merge or deployment.
- **Recovery:** Interrupted code jobs require `/code retry` or `/code discard`.
  Interrupted `/do` jobs are not rerun automatically because they may have
  produced partial or non-idempotent changes.

## Safety

> **Warning:** `/ask`, `/brainstorm`, `/code`, `/do`, and `/goal` run Codex with full host filesystem
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

Durable `/do` jobs and persistent `/goal` work require a separate worker:

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
