# Telegram Project Manager

Lean Telegram bot platform for project-management bots. The first bot is a GitHub commit manager controlled from Telegram.

## Runtime

- Python 3.11+
- `uv`
- LangChain `ChatOpenAI`
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
/branch <branch_name>           Set this chat's default branch (admin)
```

### Commit workflow

```text
/commit <request>              Generate a commit plan (admin)
/confirm <plan_id>             Execute a commit plan (admin)
/cancel <plan_id>              Cancel a plan (requester or admin)
```

### Configuration

```text
/config show                                Show redacted configuration (admin)
/config set openai_api_key <key>            Set API key (admin, private chat only)
/config set openai_base_url <url>           Set OpenAI-compatible URL (admin)
/config set openai_model <model>            Set model (admin)
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
LLM requests use LangChain's `langchain-openai` integration in JSON mode.
LangChain message history is stored in SQLite per Telegram chat. The latest 12 messages are retained by default; admins can change the limit or clear the current chat's memory.
