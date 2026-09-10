# Wispbyte deployment: NERP Forms BOT

Use this guide for the **Altitude Government test bot** first. Do not deploy to the
live NERP DOJ server until the complete test workflow has passed.

## 1. Prepare the Wispbyte server

In the Wispbyte panel, select a current **Python 3.12** image or newer. This project
does not need an inbound web port for its Discord Gateway connection.

Upload the project files, excluding `.venv/`, `.env`, `data/`, and `secrets/`. The
upload must preserve the top-level layout:

```text
config/
src/
requirements.txt
pyproject.toml
```

## 2. Configure startup

Set Wispbyte's additional Python packages field to the contents of `requirements.txt`,
or install the packages through the server console once:

```text
pip install -r requirements.txt
```

Set the startup command to:

```text
PYTHONPATH=src python -m nerp_forms_bot.main
```

If the panel does not accept the `PYTHONPATH=...` prefix, use its environment-variable
section to set `PYTHONPATH` to `src`, then set the startup command to:

```text
python -m nerp_forms_bot.main
```

## 3. Set protected environment variables

Create these in Wispbyte's Startup / Environment Variables settings:

| Name | Value for first test deployment |
| --- | --- |
| `DISCORD_TOKEN` | The bot token, entered directly in Wispbyte—not in chat or source files |
| `NERP_ENVIRONMENT` | `test` |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/nerp_forms_bot.db` |
| `GOOGLE_FORMS_POLL_INTERVAL_SECONDS` | `60` |

Do **not** create `GOOGLE_SERVICE_ACCOUNT_FILE` yet. It will be added after the Google
Cloud service account and its Drive/Sheets/Docs access are configured.

## 4. First startup check

Start the server and inspect the Wispbyte console. A successful boot should report that
the bot connected, with its Discord account name and ID. In Altitude Government, run:

```text
/bot-status
```

The bot must reply privately that it is connected to **Altitude Government** using the
**test** configuration profile.

## Troubleshooting

- **Module not found:** confirm that the project was uploaded with `src/` intact, that
  Python packages are installed, and that `PYTHONPATH=src` is set.
- **Invalid token:** regenerate the token in the Discord Developer Portal, replace it
  only in Wispbyte's protected variable, and restart. Never paste it into chat.
- **Bot starts but `/bot-status` is absent:** wait briefly for Discord to register the
  test-server command, then restart once. Confirm the bot is installed in Altitude
  Government and `NERP_ENVIRONMENT=test` is present.
- **Permission error in a channel:** verify the bot's per-category/channel permissions;
  it should not need Administrator.
