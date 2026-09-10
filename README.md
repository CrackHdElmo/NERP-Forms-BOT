# NERP Forms BOT

NERP Forms BOT is a configuration-driven Discord workflow service for the New Era RP
Department of Justice. It turns approved Google Form submissions into trackable Discord
work items, maintains Google Sheets/Drive records, and supports role-verified DOJ actions.

## Initial Discord design

| Workflow | Discord destination | Visibility model |
| --- | --- | --- |
| Service directory | `#service-desk` text channel | Read-only, bot-managed links and instructions |
| Active criminal/civil case | `docket` Forum Channel | Shared staff operational record |
| Off-docket request | Bot-created text channel in Off-Docket Requests | Per-ticket role and user permission overwrites |
| Attorney request | Bot-created text channel in Attorney Requests | Per-ticket role and user permission overwrites |
| Business licensing | `business-licensing` Forum Channel | Shared Corporate Office workflow initially |

The bot must never use a Discord text channel as the destination for an active docket.
Each docket is a post in the configured `docket` Forum Channel.

## Environments

- `config/environments/test.yaml` is the Altitude Government test profile. It contains
  Discord resource identifiers, which are not secrets.
- `config/environments/production.example.yaml` documents the live NERP DOJ role design.
  It intentionally has no live server/category/channel IDs yet.
- `.env` contains local or host-provided secrets and is ignored by Git. Start from
  `.env.example`; never send or commit its populated values.

## Local setup

1. Install Python 3.12 or later.
2. Create and activate a virtual environment.
3. Install the package with `pip install -e .[dev]`.
4. Copy `.env.example` to `.env` and set values directly in the local protected file or
   in the hosting provider's secret manager. Do not share them in chat.
5. Run `nerp-forms-bot`.

The initial slash-command surface is deliberately small: `/bot-status` verifies the
selected environment and confirms that the bot is connected to the expected guild.
Google intake, document generation, and ticket/docket creation will be added only after
the Google integration and template decisions are configured.

## Deployment

`Dockerfile` and `compose.yaml` provide a portable starting point for Wispbyte or another
container-capable host. Supply environment variables through the host's protected settings.
Do not bake credentials into the image, repository, or compose file.

For Wispbyte's managed Python server image, follow [the Wispbyte deployment guide](docs/WISPBYTE_DEPLOYMENT.md).
