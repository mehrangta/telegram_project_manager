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
/repo show                                    Show chat repository settings
/repo allow owner/repository                  Allow repository (admin)
/repo disallow owner/repository               Disallow repository (admin)
/repo set owner/repository                    Set active repository (admin)
/repo clear                                   Clear active repository (admin)
/repo local set <absolute-path>               Set managed Git cache (admin)
/repo local clear                             Clear managed Git cache (admin)
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
/deploy <c-job_id>                            Confirm merge and deployment

/config show                                  Show redacted configuration
/memory status | /memory show                 Show chat memory usage
/memory clear                                 Clear chat memory (admin)
/admin add <telegram_user_id>                 Add admin
/admin remove <telegram_user_id>              Remove admin
~~~

## Workflows

### Issues

/issue uses the chat's active repository and managed cache to build a
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

### Merge and deployment

/deploy requires confirmation, a ready PR targeting main, and the exact head
SHA accepted by CI. It honors reviews, branch protection, and merge queues;
squash-merges, deletes the branch, dispatches the configured workflow_dispatch
workflow at the merge SHA, and monitors it for up to 30 minutes.

The workflow must accept a required ref input. The bot allows two minutes for
the dispatched run to appear and resumes active deployment monitoring after a
service restart.

## Safety

- Repository allowlist and per-chat active repository
- Service-owned Git caches with strict origin verification
- Isolated worktrees and Codex sandboxes
- No changes to .env files, private keys, or .github/workflows
- Maximum 100 changed files and 5 MB per Codex job
- Host-owned commits, pushes, pull requests, and deployment
- API keys redacted from configuration and progress output

## Managed repository cache

The cache must be an absolute, writable normal or bare Git repository whose
literal origin matches owner/repository. The bot never falls back to cloning.

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
