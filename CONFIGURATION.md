# NERP - Case Management configuration map

This is the root-level starting point for an administrator or developer preparing a new NERP - Case Management deployment. The private deployment repository supports one authoritative file, `config/master.yaml`, for every runtime setting, token, server/resource ID, role mapping, and workflow option. The only exception is the Google service-account JSON file itself, which remains a host file and is referenced by its path in the master configuration.

The public source repository contains only [the blank master template](config/master.example.yaml). Never add the populated `config/master.yaml` to the public repository.

## Private one-file deployment configuration

In the private deployment repository, copy `config/master.example.yaml` to `config/master.yaml` and fill it once. When this file exists, the bot loads it instead of `.env` and `config/environments/<name>.yaml`.

```text
config/master.yaml
├── runtime       tokens, database URL, Google credential-file path, FiveManage settings
└── environment   Discord server, categories, channels, roles, Forms, Sheets, Drive, templates, workflows
```

The file is intentionally listed in `.gitignore` for the public source repository. The private deployment repository tracks its own populated copy so an approved source-control push can carry the complete deployment state to Wispbyte.

## Legacy/profile-based configuration

The active profile is selected by `NERP_ENVIRONMENT` in the protected runtime settings. It loads this file:

```text
config/environments/<NERP_ENVIRONMENT>.yaml
```

Use `config/environments/test.yaml` as the working reference for the complete supported shape only when the private master configuration is not present. For a profile-based deployment, copy `config/environments/production.example.yaml` to `config/environments/production.yaml`, replace every `null` or sample value with the live resource ID, then set `NERP_ENVIRONMENT=production` in Wispbyte.

Do not commit `production.yaml` if it contains IDs or deployment details your organization treats as private. Git ignores ordinary `.env` secrets, but review every new file before pushing.

## What belongs in the environment YAML

This is the main non-secret configuration file for the legacy/profile-based method. Values in it take effect after the bot restarts. In the private one-file method, the same sections sit beneath `environment:` in `config/master.yaml`.

| YAML section | Configure here |
| --- | --- |
| `environment` | Profile label shown by `/bot-status`, such as `test` or `production`. |
| `guild` | Discord server ID and display name. |
| `channels` | Existing Service Desk, Docket Forum, and Business Licensing Forum IDs. |
| `categories` | Court Administration, Off-Docket, Attorney Requests, and Corporate Office category IDs. |
| `roles` | Administrator, Judge, Attorney General, DOJ/PD Command, prosecutor, defense-attorney, PD-officer, and other workflow role IDs. A role list is used instead of a display name so permissions stay correct after a role is renamed. |
| `workflows` | Destination key, Discord resource type, and first status for each intake workflow. |
| `google_workspace` | Shared Drive folder ID used for tracking/workspace files. |
| `court_order_intake` | Case Management System Form URL, response spreadsheet ID, and response-tab name. |
| `court_order_warrant` | Arrest Warrant Google Doc template ID and generated-file destination folder ID. |
| `court_order_search_seizure_warrant` | Search / Seizure template and output-folder IDs. |
| `court_order_subpoena` | Subpoena template and output-folder IDs. |
| `test_requester_user_id` | Optional test-only starter user ID. Omit it from a live profile unless it is deliberately needed. |

### Minimum role mapping

At a minimum, a live profile needs role-ID lists for these keys when their related features are used:

```yaml
roles:
  administrators: []
  judges: []
  attorney_general: []
  prosecutors: []
  defense_attorneys: []
  pd_officers: []
  doj_command: []
  pd_command: []
```

Keep any additional organization-specific role mappings alongside them. The bot never grants a Discord role; it only checks the server roles already assigned by your staff.

## Protected Wispbyte or local runtime settings

For the legacy/profile-based method, start from [`.env.example`](.env.example). In Wispbyte, add these as **protected environment variables** rather than making an uploaded `.env` file. In the private one-file method, these settings belong under `runtime:` in `config/master.yaml`; the Google service-account JSON remains outside Git and is referenced by `google_service_account_file`.

| Setting | Required | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | Yes | Discord application bot token. |
| `NERP_ENVIRONMENT` | Yes | Profile name, normally `test` or `production`. |
| `DATABASE_URL` | Yes | Durable bot state: tickets, assignments, Service Desk configuration, and direct bot-admin grants. Back it up before major changes. |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | For Form/Drive workflows | Secure path to the Google service-account JSON file on the host. |
| `GOOGLE_FORMS_POLL_INTERVAL_SECONDS` | No | Form polling interval; `60` is the default. |
| `FIVEMANAGE_API_TOKEN` | No | Optional FiveManage upload token. |
| `FIVEMANAGE_STORAGE_PATH` | No | Optional FiveManage folder/path for generated media. |

### Google service-account file path

`google_service_account_file` is **not** the Google API key, a token, the Google account email,
or the contents of the downloaded JSON. It is only the absolute file path on the host where the
Google service-account JSON credential is securely stored. The JSON file gives the bot access to
the Google Form response Sheet, Google Docs templates, and Shared Drive resources that were
shared with the service account.

Keep the JSON file outside the repository and outside the `data/` directory. Create a host-only
`secrets` folder, limit it to the account that runs the bot, and point the master configuration at
that file:

| Host | Example `google_service_account_file` value |
| --- | --- |
| Current Wispbyte server | `/home/container/secrets/google-service-account.json` |
| Linux VPS or dedicated server | `/opt/nerp/secrets/google-service-account.json` |
| Windows VPS or dedicated server | `C:/NERP/secrets/google-service-account.json` |

When migrating hosts, copy the existing JSON securely to the new host's protected `secrets`
folder and change only this path in the new host's private `config/master.yaml`. Do not commit the
JSON file, expose it in a screenshot, or paste it into a support request.

## Items administrators can change without editing YAML

These values are stored in the bot database and survive restarts. They are managed through Discord commands, so they should not be added to the YAML profile.

| Item | Command |
| --- | --- |
| Server resource setup/registration | `/setup-workflow` |
| Managed `#service-desk` title, image, directory copy, four form links, and four external links | `/service-desk` command group |
| Individual, direct NERP bot administrators | `/bot-admin add`, `/bot-admin remove`, and `/bot-admin list` |

Direct `/bot-admin` grants supplement — but do not replace — server ownership, Discord's native Administrator permission, and the YAML `roles.administrators` list. Removing a direct grant never strips any of those other privileges.

## Safe change process

1. Back up the runtime database before a major configuration or role change.
2. Update the test profile first and validate `/bot-status`, `/google-workspace-status`, and the affected workflow.
3. Make the matching live change in `production.yaml` and protected Wispbyte variables.
4. Push the reviewed project change to GitHub, let Wispbyte synchronize the selected branch, then restart the bot once.
5. Confirm the bot is online and that the updated slash command or workflow behaves correctly in the target server.

## Documentation maintenance rule

The project treats documentation as part of every bot feature. Whenever a command, workflow,
permission boundary, setting, host requirement, template requirement, or deployment procedure
changes, update these together before publishing the tested change:

1. `README.md` — project overview and deployment summary.
2. `CONFIGURATION.md` — authoritative configuration and host-migration reference.
3. `docs/FIRST_TIME_SETUP.md` — from-zero installation and deployment procedure.
4. `docs/BOT_REFERENCE_GUIDE.md` — staff/player commands and daily workflow guide.
5. `config/master.example.yaml` — safe, current one-file private-deployment template.

For the full first deployment walkthrough, see [docs/FIRST_TIME_SETUP.md](docs/FIRST_TIME_SETUP.md). For a player and staff command guide, see [docs/BOT_REFERENCE_GUIDE.md](docs/BOT_REFERENCE_GUIDE.md).
