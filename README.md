# NERP - Case Management

NERP - Case Management is a configuration-driven Discord workflow service for the New Era RP
Department of Justice. It turns approved Google Form submissions into trackable Discord
work items, maintains Google Sheets/Drive records, and supports role-verified DOJ actions.

## First-time installation and deployment

For a complete from-zero walkthrough covering private GitHub source control, Discord bot
creation and permissions, Google Cloud/service-account and Shared Drive setup, Wispbyte
deployment, the guided Discord resource wizard, and an end-to-end Court Order test, follow
[the first-time setup guide](docs/FIRST_TIME_SETUP.md). Keep that guide updated whenever the
bot gains a workflow, command, permission, or hosting requirement.

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
`/setup-workflow` is an administrator-only, private guided setup wizard. It uses a dropdown,
optional name-customization form, preview, and explicit confirmation before it creates anything.
Its initial presets create or safely register the Court Administration resources (Service Desk,
Docket Forum, and staff-only case records), the Off-Docket Court Orders category, the Attorney
Requests category, or the Corporate Office/Business Licensing Forum. Existing matching bot
resources are reused rather than duplicated, and their IDs are stored in the bot database so
runtime workflows can use resources created through Discord without editing GitHub files.
`/service-desk` is an administrator-only command group for maintaining one durable, bot-owned
directory embed in `#service-desk`. It can update the header, directory introduction, and image;
maintain up to four case/form links and four SOP or external-resource links; remove individual
links; privately preview the directory; and adopt a prior bot-authored Service Desk post for
future in-place updates. Every change edits the same managed post rather than creating duplicate
directory messages.
`/google-workspace-status` is restricted to configured bot administrators; it verifies
the protected Google credential and creates (or finds) the test request tracker in the
shared Drive folder. `/court-order-sync` imports new Case Management System response rows. Court
Orders create staff-only off-docket tickets, while the **File New Case: Creates New Docket Entry**
choice creates a `DCK-######` post directly in the configured Docket Forum with a party-based
title and a pending-review status. A requester then runs `/claim-court-order`; the bot verifies
their submitted Discord username through that interaction before granting ticket access.
When an active ticket needs to be repaired after a Form or display issue, a configured
administrator, Judge, or Attorney General can run `/refresh-court-order` in that ticket to
reload its original Form response and post fresh intake and subject-detail embeds without
creating a new request.
The verified requester or a configured bot administrator can use `/add-ticket-member`
inside any bot-managed private NERP request ticket to grant a selected server member permission
to view, message, and attach files there; the bot posts an in-ticket audit message for each grant.
The same command is designed for future private Attorney Request and Corporate Office ticket
workflows. Discord Forum posts, including Dockets and Business Licensing, inherit their parent
Forum permissions and cannot grant access to one individual post.

`/close-ticket` can be used by the verified requester, configured judicial roles, the configured
Attorney General role, or a server/bot administrator. On its first use, the bot creates a
read-only `#doj-case-records` channel beneath Court Administration with access limited to the
configured DOJ and PD staff roles. Each closure posts a staff synopsis there, including all
Form-submitted evidence links and any recorded approval or denial identity/notes, before it
removes the source private ticket. A tracked Forum/Docket post is archived and locked instead
of deleted so the shared docket record remains intact.

Within a Court Order ticket, a configured Judge or bot administrator can use
`/deny-arrest-warrant` with required denial notes. The denial, issuing official, and timestamp
are retained. The ticket deliberately stays open so the requester can submit a corrected Form;
entering the existing `COR-######` request ID (or the existing bot-managed ticket name) in the
Form's **Docket / Off-Docket Name/ID** answer attaches the new submission to that existing
ticket rather than creating another private channel. A later authorized Judge or administrator
may still approve the same denied request; the permanent closure record retains both the earlier
denial and the later approval.

All Court Order tickets, new Docket filing posts, and permanent case-record embeds display the
submitted requester name, requester role, agency, primary case officer, and case-officer agency.
The bot also checks the linked response Sheet automatically every 60 seconds by default;
`/court-order-sync` remains available to administrators as an immediate reconciliation and
retry command. Set `GOOGLE_FORMS_POLL_INTERVAL_SECONDS` to a larger value (for example,
`120`) in protected host settings when a slower polling interval is preferred.

## Docket merges and case assignments

An authorized administrator, Judge, or Attorney General can run
`/merge-court-order-to-docket` from either side of an active case relationship without using
Discord's internal channel IDs. In the source private Court Order ticket, provide only the
destination `docket_id` (for example, `DCK-000010`). In the destination Docket Forum post,
provide only the source `court_order_id` (for example, `COR-000007`). The command merges
immediately and leaves the original Court Order open for continuing investigative work. A
retained Court Order can be merged again later to forward only newer ticket activity to the same
Docket post.

Leadership can run `/rename-docket` inside an active Docket Forum post to change its title. A
new Docket Form filing automatically imports each valid referenced `COR-######` Court Order while
leaving its original private ticket open. To import an order later, or to close its source after a
successful import, authorized leadership can run `/import-court-order` inside the destination
Docket post and select the desired disposition.

`/assign-case` supports two separate optional record slots for each Prosecutor, Judge, Defense
Attorney, and PD Officer. Every slot is shown as a named member or **Unassigned** in the ticket.
Eligible staff may self-assign; configured DOJ/PD Command and bot administrators may assign or
reassign another eligible member. `/transfer-case-assignment` creates an acceptance-required
handoff for the selected slot, and the recipient must run `/accept-case-transfer` in the same
ticket before that slot changes. The permanent closure record includes all final assignments and
accepted transfers.

For a claimed Arrest Warrant request, a configured administrator can use
`/generate-arrest-warrant` with the bot-issued request ID inside that request's private ticket.
The verified requester and configured bot administrators can use it. The bot reads the submitted docket,
classification, primary-case-officer, and law-enforcement-agency answers directly from the Court
Order Form, then copies the approved Google Docs template into the protected
Shared Drive, fills the request fields, exports a PDF, and verifies its page count before it
posts anything player-facing. It posts a PNG rendering only when the source PDF is exactly one
page; the PDF remains an internal validation artifact. If a charge is
over 88 characters, the combined probable-cause statement is over 2,080 characters, or the
completed PDF cannot fit on one page, the bot does not issue a warrant and advises the verified
requester in the private Discord ticket. It never silently truncates the submitted information.

Within that request's private ticket, a configured Judge or bot administrator can run
`/approve-arrest-warrant`. The bot creates a separate approved document, replaces the approver
and issue-date placeholders, records the approval as `/s/ Judge Name`, records the approver's
Discord identity and UTC approval time, and prevents a second approval for the same request.

The same guarded workflow is available for **Search or Seizure Warrant** Form responses through
`/generate-search-seizure-warrant`, `/approve-search-seizure-warrant`, and
`/deny-search-seizure-warrant`. It supports up to three subjects, fills each subject's name,
Citizen ID, Date of Incident, and selected Search/Seizure type, and applies the same one-page PNG
and later-approval safeguards as the Arrest Warrant flow.

**Subpoena** Form responses use the same private-ticket, claim, judicial approval/denial, one-page
PNG, and FiveManage delivery path through `/generate-subpoena`, `/approve-subpoena`, and
`/deny-subpoena`. A request-type option beginning with `Subpoena` is accepted, so descriptive
Form choices such as `Subpoena - Documents/Media & Order to Appear` remain on this workflow. The
template supports up to three listed subjects and fills the exact
`{{SUBJECT_n_DATE}}`, `{{SUBJECT_n_SUBPOENA_TYPE}}`, and `{{SUBPOENA_DETAILS}}` fields used by the
native Google Doc.

When `FIVEMANAGE_API_TOKEN` is configured only in protected host settings, the bot also uploads
the approved PNG to FiveManage and posts its returned CDN URL alongside the attached PNG. A
FiveManage failure never prevents the verified PNG from being posted to the private ticket.

## Deployment

`Dockerfile` and `compose.yaml` provide a portable starting point for Wispbyte or another
container-capable host. Supply environment variables through the host's protected settings.
Do not bake credentials into the image, repository, or compose file. Wispbyte's Python
main-file selector should use the repository-root `main.py` launcher.

For Wispbyte's managed Python server image, follow [the Wispbyte deployment guide](docs/WISPBYTE_DEPLOYMENT.md),
then use [the first-time setup guide](docs/FIRST_TIME_SETUP.md) for the full cross-service
configuration and validation sequence.
