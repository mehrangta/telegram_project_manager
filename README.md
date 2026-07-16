# Telegram Project Manager

Telegram bot for GitHub issue drafting, commit planning, Codex implementation,
pull-request validation, and deployment.

## Requirements

- Python 3.11+ and uv
- GitHub CLI (gh) authenticated for the service account
- OpenAI-compatible API access
- Local or bare Git caches for managed repositories
- SQLite (./data/bot.db by default)

## Quick start

~~~powershell
uv sync
uv run telegram-project-manager init-db
uv run telegram-project-manager admin add <telegram_user_id>
uv run telegram-project-manager run
~~~

Create data/secrets.json with only the Telegram token:

~~~json
{"TELEGRAM_BOT_TOKEN":"..."}
~~~

Authenticate GitHub and configure models in a private admin chat:

~~~powershell
gh auth login
gh auth status
~~~

~~~text
/config set openai_api_key <key>
/config set openai_base_url https://api.openai.com/v1
/config set openai_model <model>
/config set codex_api_key <key>
/config set codex_base_url https://api.openai.com/v1
/config set codex_plan_model <model>
/config set codex_code_model <model>
~~~

API keys are stored separately in SQLite, redacted by /config show, and cannot
be set from group chats. codex_model remains a shared fallback when a
phase-specific Codex model is unset.

## Commands

Commands marked admin require a registered Telegram admin.

~~~text
/start | /help | help                         Show help
/status                                       Show service and GitHub status
/repos                                        List allowed repositories
/repo show                                    Show saved repository settings
/repo check                                   Validate the configured Git cache
/repo allow owner/repository                  Allow repository (admin)
/repo disallow owner/repository               Disallow repository (admin)
/repo set owner/repository                    Set active repository (admin)
/repo setup owner/repository                  Download and configure repository (admin)
/repo clear                                   Clear active repository (admin)
/repo local set <absolute-path>               Set managed Git cache (admin)
/repo local clear                             Clear managed Git cache (admin)
/repo deploy enable owner/repository          Enable manual deploy (admin)
/repo deploy disable owner/repository         Disable manual deploy (admin)
/repo deploy set owner/repository deploy.yml  Set deployment workflow (admin)
/repo deploy clear owner/repository           Clear deployment workflow (admin)
/branch <branch>                              Set default branch (admin)

/commit <request>                             Generate commit plan (admin)
/confirm <plan_id>                            Execute commit plan (admin)
/cancel <plan_id>                             Cancel commit plan

/issue <prompt>                               Draft issue (admin)
/edit <i-draft_id> <feedback>                 Revise issue draft
/confirm <i-draft_id>                         Create GitHub issue
/cancel <i-draft_id>                          Cancel issue draft

/code #123                                    Plan issue in active repository
/code owner/repository#123                    Plan issue in allowed repository
/code <GitHub issue URL>                      Plan issue from URL
/code #123 --skip-plan                        Implement immediately
/code approve <c-job_id>                      Approve implementation
/code edit <c-job_id> <feedback>              Revise plan
/code retry <c-job_id>                        Retry failed/interrupted phase
/code rebase <c-job_id>                       Rebase and rerun CI
/code discard <c-job_id>                      Close PR and delete branch
/code status [c-job_id]                       Show one or recent jobs
/ask <question> [images]                      Ask Codex about the active repository
/do <job>                                     Run unrestricted Codex job (private admin chat only)
/merge <c-job_id>                             Confirm merge without deployment
/deploy <c-job_id>                            Confirm merge and deployment

/config show                                  Show redacted configuration
/memory status | /memory show                 Show current chat/topic memory usage
/memory clear                                 Clear current chat/topic memory (admin)
/admin add <telegram_user_id>                 Add admin
/admin remove <telegram_user_id>              Remove admin
~~~

## Workflows

### Forum topics and repositories

In a Telegram supergroup with Topics enabled, repository settings are scoped
to the current topic. Each topic must run `/repo set owner/repository`
explicitly; topic settings do not inherit the group's active repository,
branch, or managed cache. Commands sent without a `message_thread_id` continue
to use group-level settings. Plans, drafts, code-job controls, deployments, and
LLM memory remain bound to the topic where they were created.

The repository allowlist, deploy-enabled flag, and per-repository deployment
workflow remain global.

### Issues

/issue uses the current chat or topic's active repository and managed cache to build a
repository-aware draft. Reply to the preview with text or images to revise it,
then confirm it. Drafts expire after one hour.

JPEG, PNG, and GIF are supported: up to 10 images, 10 MB each, and 20 MB total.
Images are stored on an isolated issue-assets branch and later supplied to
Codex as vision inputs. Set issue_body_llm_enabled=false to keep the original
prompt as the issue body and generate only a title.

### Code

/code creates an isolated worktree and draft PR. Planning runs read-only with
codex_plan_model; approved implementation, CI repair, and rebase repair use
codex_code_model.

Codex must discover validation commands from repository metadata rather than
assume scripts or tools exist. Invalid validation triggers up to two recovery
turns on the same thread. A job becomes ready only after a real command passes
and all GitHub checks for the current head pass. CI failures receive up to two
repair commits.

Two jobs may run concurrently and ten may queue. Restarts mark active turns
interrupted for explicit retry or discard. Progress cards show phases, commands,
files, recent activity, failure timing, and detailed errors without exposing raw
model reasoning.

Planning follows a repository-first, decision-complete workflow. If Codex finds
material product choices it cannot resolve from the repository, the draft PR and
Telegram plan-ready ping show up to three questions with recommended options.
Approval remains blocked until the questions are resolved. Reply to the Telegram
ping or comment on the draft PR using the GitHub account authenticated by the
service; the answer is deduplicated, applied to the committed main plan, and
published as a new plan revision automatically.

### Repository questions

`/ask <question>` queues an independent, read-only Codex inspection of the
current chat or topic's active repository and default branch. A photo, image
document, or album can be attached when the command and question are provided
in the media caption. JPEG, PNG, and GIF images are supported, with a maximum
of 10 images, 10 MB per image, and 20 MB total. The bot replies immediately
with an acknowledgment, refreshes the configured managed cache, and then
replies to the original command with a concise answer and supporting repository
paths. Ask sessions are not conversational and are not resumed after a service
restart. Image support uses the configured Codex plan model and requires no
additional settings or database migration.

### Full-access jobs

`/do <job>` sends the job text directly to the configured Codex coding model
with unrestricted filesystem access. It is accepted only from registered admins
in private chats, starts immediately without confirmation, and uses the bot
service process working directory without requiring an active repository.

Full-access jobs run one at a time and reply to the original command with the
plain Codex result. They are kept only in memory and are not resumed after a
service restart because the requested work may already have produced partial or
non-idempotent side effects.

### Merge and deployment

/merge requires confirmation and a ready code job. It revalidates the exact
checked pull-request head, reviews, checks, mergeability, and configured base
branch before squash-merging and deleting the source branch. It never starts a
deployment workflow. Merge-only operations can target any configured base
branch and resume safely after a bot restart.

/deploy is disabled for every repository by default. An admin enables or
disables it with \`/repo deploy enable owner/repository\` and \`/repo deploy
disable owner/repository\`. Enabling it only exposes and permits the manual
Deploy action; it does not deploy automatically after a push.

When enabled, /deploy requires confirmation, a ready PR targeting main, and
the exact head SHA accepted by CI. It honors reviews, branch protection, and merge queues;
squash-merges, deletes the branch, dispatches the configured workflow_dispatch
workflow at the merge SHA, and monitors it for up to 30 minutes.

If `/merge` already merged a job into main, `/deploy` reuses the stored merge
SHA and starts the configured workflow without trying to merge again. Jobs
merged into another base branch remain ineligible for deployment.

The workflow must accept a required ref input. The bot allows two minutes for
the dispatched run to appear and resumes active deployment monitoring after a
service restart.

## Safety

- Repository allowlist and independent per-chat/per-topic repository context
- Service-owned Git caches with strict origin verification
- Isolated worktrees and Codex sandboxes
- `/do` is an explicit private-admin-only exception that runs Codex with full host access
- No changes to .env files, private keys, or .github/workflows
- Maximum 100 changed files and 5 MB per Codex job
- Host-owned commits, pushes, pull requests, and deployment
- API keys redacted from configuration and progress output

## Managed repository cache

Run /repo setup owner/repository to let the bot verify GitHub access, detect
the default branch, and create or reuse a bare cache under the database
directory's repos folder. Setup runs in the background and updates the current
chat or topic only after the cache has been validated and refreshed.

The service account must be authenticated with gh. Existing matching caches are
reused; an invalid or mismatched destination is never deleted automatically.

For manual setup, the cache must be an absolute, writable normal or bare Git
repository whose literal origin matches owner/repository.

Example bootstrap from an existing checkout:

~~~bash
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
~~~

Then configure the path with /repo local set <absolute-path>.

## CLI and service

~~~text
telegram-project-manager [--db <path>] init-db
telegram-project-manager [--db <path>] run
telegram-project-manager [--db <path>] admin add|remove ...
telegram-project-manager [--db <path>] config show
telegram-project-manager [--db <path>] config set <key> <value>
~~~

Supported configuration keys:

~~~text
openai_api_key, openai_base_url, openai_model
codex_api_key, codex_base_url, codex_model
codex_plan_model, codex_code_model
issue_body_llm_enabled, llm_memory_max_messages
max_files_per_commit, max_bytes_per_commit, require_confirmation
~~~

VPS operations:

~~~bash
sudo systemctl enable --now telegram-project-manager
sudo systemctl restart telegram-project-manager
sudo systemctl stop telegram-project-manager
sudo systemctl status telegram-project-manager
sudo journalctl -u telegram-project-manager -f
sudo -u telegram-pm -H gh auth login --hostname github.com --web
sudo -u telegram-pm -H gh auth status --hostname github.com
~~~
