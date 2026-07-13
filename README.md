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
/config set openai_api_key <key> (private chat only)
/config set openai_base_url <url>
/config set openai_model <model>
/config set llm_memory_max_messages <count>
/memory status
/memory clear
```

OpenAI credentials and provider configuration are stored in SQLite and managed through admin `/config set` commands. Only the Telegram bot token lives in `data/secrets.json`.
LLM requests use LangChain's `langchain-openai` integration in JSON mode.
LangChain message history is stored in SQLite per Telegram chat. The latest 12 messages are retained by default; admins can change the limit or clear the current chat's memory.
