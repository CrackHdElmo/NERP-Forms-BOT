# Wispbyte deployment: NERP Forms BOT

Use this guide for a **test bot** first. Do not deploy to a live server until the
complete test workflow has passed. For the full Discord, Google, GitHub, and test
workflow walkthrough, start with the [first-time setup guide](FIRST_TIME_SETUP.md).

## 1. Prepare the Wispbyte server

In the Wispbyte panel, select a current **Python 3.12** image or newer. This project
does not need an inbound web port for its Discord Gateway connection. Connect Wispbyte
to the project's **private GitHub repository** and select the intended branch (normally
`main`). Repository sync is the supported deployment path; do not repeatedly replace the
server with manual ZIP uploads.

## 2. Configure startup

Set Wispbyte's additional Python packages field to the contents of `requirements.txt`,
or install the packages through the server console once:

```text
pip install -r requirements.txt
```

Select the repository-root `main.py` when Wispbyte asks for the main file. It
is a lightweight launcher that starts the packaged application under `src/`.

Set the startup command to:

```text
python main.py
```

## 3. Set protected environment variables

Create these in Wispbyte's Startup / Environment Variables settings:

| Name | Value for first test deployment |
| --- | --- |
| `DISCORD_TOKEN` | The bot token, entered directly in Wispbyte—not in chat or source files |
| `NERP_ENVIRONMENT` | `test` |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/nerp_forms_bot.db` |
| `GOOGLE_FORMS_POLL_INTERVAL_SECONDS` | `60` |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `/home/container/secrets/google-service-account.json` |

Upload the service-account JSON file directly to Wispbyte's ignored `secrets/` folder.
Do not commit it to GitHub, upload it through source control, or paste its contents into
chat. The test Drive folder ID belongs in `config/environments/test.yaml`; it is an
identifier, not a credential.

## 4. First startup check

Start the server and inspect the Wispbyte console. A successful boot should report that
the bot connected, with its Discord account name and ID. In Altitude Government, run:

```text
/bot-status
```

The bot must reply privately that it is connected to **Altitude Government** using the
**test** configuration profile.

After the Google credential file and test Drive folder are configured, a test-role
administrator can run `/google-workspace-status`. The command creates or finds the
single request tracker and returns its Google Sheets link.

After every code/configuration push, first confirm Wispbyte synchronized the selected
GitHub branch, then restart the server. The running process does not automatically load
new source solely because it was pushed to GitHub.

## Troubleshooting

- **Module not found:** confirm that Wispbyte synchronized the intended repository branch,
  that Python packages are installed, and that the repository-root `main.py` is selected.
- **Invalid token:** regenerate the token in the Discord Developer Portal, replace it
  only in Wispbyte's protected variable, and restart. Never paste it into chat.
- **Bot starts but `/bot-status` is absent:** wait briefly for Discord to register the
  test-server command, then restart once. Confirm the bot is installed in Altitude
  Government and `NERP_ENVIRONMENT=test` is present.
- **Permission error in a channel:** verify the bot's per-category/channel permissions;
  it should not need Administrator. The guided setup and ticket workflows require
  **Manage Channels**, **Manage Roles**, and **Manage Threads**. Keep the bot role above
  any roles whose access it must manage.
- **Form submission has not appeared yet:** the bot checks the linked response Sheet every
  60 seconds by default. An administrator can use `/court-order-sync` for an immediate
  retry, or set `GOOGLE_FORMS_POLL_INTERVAL_SECONDS=120` in Wispbyte Environment Variables
  to use a two-minute interval.
