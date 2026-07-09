# Telegram Project Manager

Lean Telegram bot platform for project-management bots. The first bot is a GitHub commit manager controlled from Telegram.

## Runtime

- Python 3.11+
- `uv`
- Telethon
- GitHub CLI (`gh`) installed and authenticated
- SQLite database at `./data/bot.db` by default

## Setup

```powershell
uv sync
uv run telegram-project-manager init-db
uv run telegram-project-manager admin add <telegram_user_id>
uv run telegram-project-manager run
```

Secrets come from environment variables or `./data/secrets.json`:

```json
{
  "TELEGRAM_API_ID": "12345",
  "TELEGRAM_API_HASH": "...",
  "TELEGRAM_BOT_TOKEN": "...",
  "OPENAI_API_KEY": "..."
}
```

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
```

Normal configuration is stored in SQLite. Secrets are not stored in SQLite.

