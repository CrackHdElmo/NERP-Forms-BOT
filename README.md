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
`/google-workspace-status` is restricted to configured bot administrators; it verifies
the protected Google credential and creates (or finds) the test request tracker in the
shared Drive folder. `/court-order-sync` imports new Court Order Form response rows into
staff-only off-docket tickets. A requester then runs `/claim-court-order`; the bot verifies
their submitted Discord username through that interaction before granting ticket access.
The bot also checks the linked response Sheet automatically every 60 seconds by default;
`/court-order-sync` remains available to administrators as an immediate reconciliation and
retry command. Set `GOOGLE_FORMS_POLL_INTERVAL_SECONDS` to a larger value (for example,
`120`) in protected host settings when a slower polling interval is preferred.

For a claimed Arrest Warrant request, a configured administrator can use
`/generate-arrest-warrant` with the request ID and the staff-controlled docket, classification,
and case-officer details. The bot copies the approved Google Docs template into the protected
Shared Drive, fills the request fields, exports a PDF, and verifies its page count before it
posts anything player-facing. It posts the PDF only when it is exactly one page. If a charge is
over 88 characters, the combined probable-cause statement is over 2,080 characters, or the
completed PDF cannot fit on one page, the bot does not issue a warrant and advises the verified
requester in the private Discord ticket. It never silently truncates the submitted information.

## Deployment

`Dockerfile` and `compose.yaml` provide a portable starting point for Wispbyte or another
container-capable host. Supply environment variables through the host's protected settings.
Do not bake credentials into the image, repository, or compose file. Wispbyte's Python
main-file selector should use the repository-root `main.py` launcher.

For Wispbyte's managed Python server image, follow [the Wispbyte deployment guide](docs/WISPBYTE_DEPLOYMENT.md).
