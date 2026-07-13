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
uv run telegram-project-manager config set openai_base_url https://api.openai.com/v1
uv run telegram-project-manager config set openai_model <model>
uv run telegram-project-manager run
```

Runtime credentials and provider settings can come from environment variables or `./data/secrets.json`:

```json
{
  "TELEGRAM_BOT_TOKEN": "...",
  "OPENAI_API_KEY": "...",
  "OPENAI_BASE_URL": "https://api.openai.com/v1"
}
```

Only the bot token from BotFather is required. Telegram API ID and API hash are not used.

GitHub auth is handled by `gh`, not by the bot:

```powershell
gh auth login
gh auth status
```

## Key Commands

```text
/status
/repo allow owner/repository
/repo set owner/repository
/repo show
/commit <request>
/confirm <plan_id>
/cancel <plan_id>
/config show
/config set openai_base_url <url>
/config set openai_model <model>
/config set llm_memory_max_messages <count>
/memory status
/memory clear
```

Normal configuration is stored in SQLite. Secrets are not stored in SQLite.
Configuration can be changed from either the CLI or the Telegram `/config set` command.
When set, `OPENAI_BASE_URL` from the environment or secrets file overrides the SQLite value.
LLM requests use LangChain's `langchain-openai` integration in JSON mode.
LangChain message history is stored in SQLite per Telegram chat. The latest 12 messages are retained by default; admins can change the limit or clear the current chat's memory.
