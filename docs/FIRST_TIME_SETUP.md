# First-time setup guide

This guide takes a new administrator from an empty Discord application to a
working **test** deployment of NERP Forms BOT. Complete the test workflow
before configuring a live community.

The currently implemented player-facing document workflows are the **Arrest
Warrant**, **Search or Seizure Warrant**, and **Subpoena** options within the Case Management
System Form.
The expanded Court Order Form may receive other request types and the bot will
preserve them in their private tickets, but it clearly identifies that their
dedicated document-generation and judicial commands are not configured yet. The Discord setup wizard can also
create and register the base resources for Dockets, Attorney Requests, and
Business Licensing, but those additional form workflows should not be
represented as automated until their individual intake and review rules have
been configured and tested.

## Before you begin

You need administrator access to:

- A Discord test server and the Discord Developer Portal.
- A private GitHub repository containing this project.
- A Wispbyte Python server.
- A Google account that can create a Google Cloud project, Google Form,
  response Sheet, Shared Drive (or shared folder), and Google Docs template.
- FiveManage only if you want the optional CDN copy of approved PNG warrants.

Keep these boundaries in place from the beginning:

- Treat the Discord bot token, Google service-account JSON key, and FiveManage
  token as secrets. Put them only in Wispbyte protected settings or protected
  server files. Never paste them into Discord, chat, GitHub, screenshots, or a
  populated `.env` file.
- Keep the GitHub repository private.
- Use a dedicated **test Discord server**, test Google resources, and test
  FiveManage path first.
- Back up the Wispbyte database before recreating or deleting a server. It
  stores request state, verified requesters, closure history, and resources
  created by the Discord setup wizard.

## What the first working deployment does

For the Court Order workflow, the bot reads the linked Form response Sheet
every 60 seconds by default (or on an administrator's `/court-order-sync`),
creates a private request ticket, verifies the requester through
`/claim-court-order`, and keeps uploaded Discord evidence inside that private
ticket. It can generate a one-page Arrest Warrant PNG, obtain a judge/admin
approval, publish a FiveManage URL when configured, and post a staff-only
closure record before the source private ticket is removed.

The source of truth for code is GitHub. Wispbyte should be connected to that
repository; after each pushed update, restart the Wispbyte server so it loads
the new revision.

## 1. Create the private GitHub source repository

1. Create a **private** repository in GitHub for the bot, or create a private
   copy of this project in the organization that will own the bot.
2. Put the complete project in the repository, including `src/`, `config/`,
   `docs/`, `requirements.txt`, `pyproject.toml`, and the repository-root
   `main.py` launcher.
3. Do **not** commit `.env`, `secrets/`, `data/`, a downloaded service-account
   key, or a Discord token. The included `.gitignore` and `.env.example` show
   the intended separation.
4. Give the Wispbyte GitHub integration access only to this private repository
   (and only the branch it needs, normally `main`).

For local development, install Python 3.12 or newer, create a virtual
environment, install `pip install -e .[dev]`, and copy `.env.example` to a
local protected `.env`. Local values are for testing only; Wispbyte will use
its own protected variables.

## 2. Create and install the Discord application

### Create the bot

1. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application and give it a recognizable name.
2. Open **Bot**, create the bot user, and copy its token directly into a
   protected Wispbyte variable named `DISCORD_TOKEN`. If the token is ever
   exposed, immediately reset it in the Developer Portal and replace it in
   Wispbyte.
3. Under **OAuth2 → URL Generator**, select the `bot` and
   `applications.commands` scopes. Install the bot into the test server using
   the generated URL.

### Grant the needed permissions

The bot should have these permissions in the test server and in the categories
it manages:

- View Channels and Read Message History
- Send Messages and Embed Links
- Attach Files
- Manage Channels
- Manage Roles
- Manage Threads

The bot does not need broad Administrator permission for normal operation.
Place its Discord role high enough in the server role list to manage the
channel overwrites and roles that the workflow requires. Do not place it above
roles it should never be able to affect.

### Gather non-secret Discord IDs for the configuration

Enable **Developer Mode** in Discord's Advanced settings. Right-click the test
server and each applicable staff role, then choose **Copy ID**. These IDs are
not secrets; they allow the bot to make exact permission decisions without
depending on changeable display names.

At minimum, the active test profile needs:

- The test server ID and server name.
- An `administrators` role ID list.
- A `judges` role ID list if judicial approvals are enabled.
- Other staff role ID lists used by your workflow (for example, prosecutors or
  defense attorneys).

## 3. Configure the Discord environment profile

The bot loads `config/environments/<environment>.yaml`, selected by
`NERP_ENVIRONMENT`. Start with `config/environments/test.yaml` and replace its
sample identifiers with your own test values.

The profile contains non-secret resource mappings such as the server ID, staff
role IDs, category/channel IDs, Google Form response Sheet ID, and Google Doc
template ID. It must remain valid YAML and match the keys expected by the
application. Do not put tokens or service-account JSON contents in it.

For a new deployment, configure the `guild` and staff `roles` first. Category
and channel IDs can be supplied in the file or safely created and remembered
by the guided Discord setup wizard in the next section. The wizard does not
modify GitHub configuration; it records the created resource IDs in the bot's
database and reuses matching resources rather than duplicating them.

`config/environments/production.example.yaml` is a role-design reference, not
a ready-to-run live profile. Before a live deployment, create and validate a
complete `production.yaml` that matches the active configuration schema and
contains approved live resource mappings.

## 4. Create the base Discord layout with the wizard

After the bot is online in the test server, run `/setup-workflow` as a server
administrator. The command responds privately and guides you through:

1. Selecting a preset.
2. Reviewing the planned category/channel/forum names.
3. Optionally customizing those names.
4. Explicitly confirming creation.

The initial presets are:

| Preset | Resources created or registered |
| --- | --- |
| Court Administration | `Court Administration`, `#service-desk`, `#docket` Forum, and staff-only `#doj-case-records` |
| Off-Docket Court Orders | private `Off-Docket Requests` category for bot-created Court Order tickets |
| Attorney Requests | private `Attorney Requests` category |
| Business Licensing | `Corporate Office` category and `#business-licensing` Forum |

Use the Court Administration and Off-Docket Court Orders presets for the
current Court Order test. `#service-desk` is intentionally read-only. Active
dockets are Forum posts in `#docket`; confidential court-order tickets are
private text channels under Off-Docket Requests. A Forum post cannot be made
visible to one individual only, so it is not suitable for confidential ticket
access.

If the wizard reports a permission issue, check the bot role's **Manage
Channels**, **Manage Roles**, and **Manage Threads** permissions and its
position in the Discord role list, then retry.

## 5. Set up Google Cloud and the service account

### Create the project and enable APIs

1. Create a dedicated Google Cloud project for the bot.
2. In **APIs & Services → Library**, enable all three APIs:
   - Google Sheets API
   - Google Drive API
   - Google Docs API
3. In **IAM & Admin → Service Accounts**, create a service account for the
   bot, then create a JSON key for it.
4. Download the key once and store it only in a protected Wispbyte server path,
   for example `/home/container/secrets/google-service-account.json`.

The key download is the only time Google provides its private key. Do not
commit it, paste it into chat, or attach it to a ticket. If it is exposed,
delete that key in Google Cloud, create a replacement, and update Wispbyte.

### Use a Shared Drive for bot-owned output

Service accounts do not have normal personal Google Drive storage. Use a
Shared Drive (recommended) or a sufficiently shared normal folder, then share
the required folders and template with the service-account email. For Shared
Drive use, give the service account a role sufficient to create, copy, export,
and update the bot's documents (normally Content manager, subject to your
organization policy).

Create or identify:

- A Shared Drive folder for request trackers and generated warrant records.
- A Shared Drive folder for player-facing warrant output.
- A Google Docs Arrest Warrant template shared with the service account.
- A Google Form and its linked Google Sheets response spreadsheet.

Google Forms with **File upload** questions cannot be stored in a Shared Drive.
For this workflow, accept evidence as links on the Form and have requesters
upload documents, screenshots, media, and files directly to their private
Discord ticket after claiming it. This keeps sensitive evidence separate from
the player-facing warrant image.

### Configure the Court Order Form and response Sheet

Connect the Form to a Google Sheets response workbook. Add the workbook ID and
the exact response tab name (normally `Form Responses 1`) to
`court_order_intake` in the test environment profile.

The current parser recognizes human-readable Form question headings. Keep the
meaning of these headings intact; repeated subject sections may receive Google
Forms suffixes such as `(2)` automatically:

- Discord username / Discord name
- Requester name, requester role, requester title, and requesting agency
- Request type
- Docket / Off-Docket Name/ID
- Subject / Arrestee name and Citizen ID
- Initial charges
- Probable cause for arrest
- Evidence links for each subject
- Primary case officer and the assigned case-officer agency
- Classification / classified designation

### Configure the new Docket filing route

Use the exact main **Request Type** choice **`File New Case: Creates New Docket Entry`** for a
new Docket. Its conditional branch must collect `Select Court:`, Petitioner Subject 1 Name,
optional Petitioner Subject 2 Name, Respondent Subject 1 Name, optional Respondent Subject 2
Name, Trial Type, Initial Hearing Type, Proposed Initial Hearing Date and Time, party-availability
status, and up to three `Import Off-Docket Court Order` references.

The bot reads those headings from the same response Sheet, creates a `DCK-######` Forum post in
the configured `docket_forum`, posts the filing overview and case-assignment record, and marks it
Pending Review. It builds the post title from the Docket ID and first petitioner/respondent names,
so a separate case-caption question is optional rather than required. The Docket Forum's parent
permissions govern who can read and participate in the post.

Any valid `COR-######` reference entered in an `Import Off-Docket Court Order` field is copied
into the new Docket automatically, including its source messages and attachments. Automatic
imports keep the original private Court Order open. A configured administrator, Judge, or Attorney
General can use `/import-court-order` inside the Docket post later and choose whether to keep or
close the original. The same leadership roles can use `/rename-docket` in an active Docket post
to apply a clearer case title.

If you redesign the wording substantially, submit a test response and confirm
that the bot identifies every field before using it operationally.

For an existing ticket, requesters can enter the bot-issued `COR-######` ID,
an existing ticket name, or an already-open Docket/Off-Docket reference in the
Form's Docket / Off-Docket field. The bot will reuse the matching open request
rather than opening a duplicate private ticket.

## 6. Prepare the Court Order warrant templates

Use native **Google Docs**, not Word files, as the active templates. Share each
template with the service account. Configure the Arrest Warrant under
`court_order_warrant`, the Search / Seizure Warrant under
`court_order_search_seizure_warrant`, and the Subpoena under `court_order_subpoena`. Each config block needs its template
document ID and the Shared Drive output-folder ID.

The template uses the following exact placeholders:

```text
{{REQUEST_ID}}              {{DOCKET_ID}}              {{CLASSIFIED}}
{{REQUESTER_NAME}}          {{REQUESTING_AGENCY}}
{{PCO}}                     {{CASE_OFFICER_AGENCY}}
{{PROBABLE_CAUSE}}
{{SUBJECT_1_NAME}}          {{S1ID}}                   {{SUBJECT_1_INITIAL_CHARGES}}
{{SUBJECT_2_NAME}}          {{S2ID}}                   {{SUBJECT_2_INITIAL_CHARGES}}
{{SUBJECT_3_NAME}}          {{S3ID}}                   {{SUBJECT_3_INITIAL_CHARGES}}
{{APPROVER_NAME}}           {{ISSUE_DATE}}
```

Put the three subject rows and the probable-cause area in ordinary Google Docs
tables, not text boxes. The final document must be one page. The bot rejects
rather than truncates a result when any initial charge exceeds 88 characters,
the combined probable-cause text exceeds 2,080 characters, or the rendered PDF
has more than one page. It produces the player-facing **PNG** only after that
one-page verification.

For electronic approval, leave `/s/ {{APPROVER_NAME}}` on the signature line
and place `{{APPROVER_NAME}}, Judge` and `{{ISSUE_DATE}}` in the related
signature/date fields. The bot creates a distinct approved copy of the
document; it does not overwrite the master template.

The Search / Seizure template uses the shared header, probable-cause, and
approval placeholders above, plus these exact table placeholders:

```text
{{SUBJECT_1_NAME}}          {{S1ID}}                   {{SUBJECT_1_INCIDENT_DATE}}  {{SUBJECT_1_WARRANT_TYPE}}
{{SUBJECT_2_NAME}}          {{S2ID}}                   {{SUBJECT_2_INCIDENT_DATE}}  {{SUBJECT_2_WARRANT_TYPE}}
{{SUBJECT_3_NAME}}          {{S3ID}}                   {{SUBJECT_3_INCIDENT_DATE}}  {{SUBJECT_3_WARRANT_TYPE}}
```

Its Form responses must use **Search or Seizure Warrant** as the main request
type, with the per-subject `Request Type:` answer set to **Search** or
**Seizure**. The bot refuses to generate if any required placeholder is missing
from the template, the combined probable cause is over 2,080 characters, or the
finished document would not fit on exactly one page.

The Subpoena template uses the shared header and signature placeholders above,
plus these exact table placeholders:

```text
{{SUBJECT_1_NAME}}          {{S1ID}}                   {{SUBJECT_1_DATE}}  {{SUBJECT_1_SUBPOENA_TYPE}}
{{SUBJECT_2_NAME}}          {{S2ID}}                   {{SUBJECT_2_DATE}}  {{SUBJECT_2_SUBPOENA_TYPE}}
{{SUBJECT_3_NAME}}          {{S3ID}}                   {{SUBJECT_3_DATE}}  {{SUBJECT_3_SUBPOENA_TYPE}}
{{SUBPOENA_DETAILS}}
```

Its Form responses must use a request-type option that begins with
**Subpoena**. This permits descriptive choices such as **Subpoena -
Documents/Media & Order to Appear** without losing the dedicated workflow. The
bot maps each subject's `Purpose of Subpoena:` response into that subject's
`SUBPOENA_TYPE` field. Evidence remains in the private Discord ticket and its
staff closure record rather than appearing on the player-facing PNG.

## 7. Connect Wispbyte to GitHub and add protected values

1. Create a Wispbyte server with Python 3.12 or later. The Discord bot uses an
   outbound Gateway connection and does not need an inbound web port.
2. Connect the server to the private GitHub repository and select the intended
   branch, normally `main`. Prefer repository sync over manual ZIP uploads.
3. Install dependencies from `requirements.txt` through Wispbyte's package
   installer or server console.
4. Set the repository-root `main.py` as the main file, with startup command:

   ```text
   python main.py
   ```

5. Create the protected environment variables below in Wispbyte. Values belong
   in Wispbyte only, never in the repository.

| Variable | First test value / purpose |
| --- | --- |
| `DISCORD_TOKEN` | Discord token for this bot application |
| `NERP_ENVIRONMENT` | `test` |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/nerp_forms_bot.db` for the test server |
| `GOOGLE_FORMS_POLL_INTERVAL_SECONDS` | `60` (or `120` for a slower interval) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `/home/container/secrets/google-service-account.json` |
| `FIVEMANAGE_API_TOKEN` | Optional protected FiveManage API token |
| `FIVEMANAGE_STORAGE_PATH` | Optional CDN folder, for example `nerp-doj/court-orders` |

6. Upload the Google service-account JSON directly to the protected Wispbyte
   path named by `GOOGLE_SERVICE_ACCOUNT_FILE`. Do not use GitHub to upload it.
7. Start or restart the server. Preserve the `data/` directory or use an
   approved persistent database location so request and setup state survive
   normal restarts.

Whenever you push a code or configuration change to GitHub, confirm Wispbyte
has synchronized it and restart the server. A GitHub push alone does not make
the running bot load new code.

## 8. First-start and end-to-end test

Run these checks in the test Discord server:

1. `/bot-status` — confirms the bot is connected to the expected guild and
   environment.
2. `/setup-workflow` — create the Court Administration and Off-Docket Court
   Orders resources if they do not already exist.
3. `/google-workspace-status` as a configured bot administrator — confirms the
   protected credential, Drive access, and tracker Sheet access.
4. Submit a **new test Court Order Form** response. Wait up to the selected
   polling interval, or use `/court-order-sync` as an administrator.
5. Verify the bot creates a private Court Order ticket and displays one
   subject-details embed per submitted subject, including each subject's
   probable cause and evidence links.
6. In that ticket, have the matching requester run `/claim-court-order`.
   Then verify `/add-ticket-member` can add an intended participant.
7. Test `/generate-arrest-warrant <request-id>` from the claimed ticket. Check
   that the posted PNG is one page, uses the expected data, and does not expose
   evidence links. If FiveManage is configured, verify its returned URL.
8. As a configured Judge or administrator, test `/approve-arrest-warrant`.
   Confirm that the approved PNG contains `/s/ Name`, the judge name, and the
   issue date.
9. On a separate test ticket, use `/deny-arrest-warrant` with notes, then test
   a later authorized approval. The final closure record must retain both
   actions.
10. Use `/close-ticket` as the verified requester, Judge, Attorney General, or
    administrator. Confirm the staff-only `#doj-case-records` embed includes
    all subject summaries, probable cause, evidence links, judicial history,
    closer, and closure note; then confirm the private source ticket was
    deleted.

Use `/court-order-baseline` only when intentionally telling the bot to mark
existing response rows as already seen. It is useful before enabling a new
environment when old Sheet rows must not create a flood of tickets.

## Day-to-day operational commands

| Command | Who can use it | Purpose |
| --- | --- | --- |
| `/setup-workflow` | Server administrator | Guided creation/registration of base Discord resources |
| `/bot-status` | Server members | Confirms active bot/environment connection |
| `/google-workspace-status` | Configured bot administrator | Verifies protected Google setup and tracker access |
| `/court-order-sync` | Configured bot administrator | Immediately import unprocessed Form responses |
| `/refresh-court-order` | Configured administrator, Judge, or Attorney General | Reloads the active ticket from its original Form response and posts refreshed intake and subject embeds without changing its request ID |
| `/merge-court-order-to-docket` | Configured administrator, Judge, or Attorney General | In an active off-docket Court Order, select a Docket Forum post by link/ID, then choose whether to keep the source open or copy and close it |
| `/import-court-order` | Configured administrator, Judge, or Attorney General | In the destination Docket Forum post, import an active `COR-######` Court Order and choose whether its source stays open or closes |
| `/rename-docket` | Configured administrator, Judge, or Attorney General | Changes the title of the active Docket Forum post and posts an audit notice |
| `/claim-court-order` | Matching requester | Claims the request in its own ticket; no numeric Discord user ID required |
| `/add-ticket-member` | Verified requester or bot administrator | Grants one server member access to the current private bot ticket |
| `/assign-case` | Eligible staff self-assignment, or configured DOJ/PD Command and administrators | Assigns or reassigns a selected first/second slot for Prosecutor, Judge, Defense Attorney, or PD Officer in the active ticket/Docket post |
| `/transfer-case-assignment` | Current assignee, configured DOJ/PD Command, or administrator | Offers a selected first/second case-role slot for handoff; the assignment remains unchanged until accepted |
| `/accept-case-transfer` | Named eligible recipient | Accepts the selected pending handoff in the same ticket or Docket post |
| `/generate-arrest-warrant` | Verified requester or bot administrator | Creates the one-page PNG warrant for the current claimed request |
| `/approve-arrest-warrant` | Configured Judge or bot administrator | Electronically approves the warrant |
| `/deny-arrest-warrant` | Configured Judge or bot administrator | Denies it with required notes; it can later be approved by an authorized reviewer |
| `/generate-search-seizure-warrant` | Verified requester or bot administrator | Creates the one-page PNG Search / Seizure Warrant for the current claimed request |
| `/approve-search-seizure-warrant` | Configured Judge or bot administrator | Electronically approves the Search / Seizure Warrant |
| `/deny-search-seizure-warrant` | Configured Judge or bot administrator | Denies it with required notes; it can later be approved by an authorized reviewer |
| `/generate-subpoena` | Verified requester or bot administrator | Creates the one-page PNG Subpoena for the current claimed request |
| `/approve-subpoena` | Configured Judge or bot administrator | Electronically approves the Subpoena |
| `/deny-subpoena` | Configured Judge or bot administrator | Denies it with required notes; it can later be approved by an authorized reviewer |
| `/close-ticket` | Verified requester, Judge, Attorney General, or administrator | Posts staff record then closes/removes private ticket |

## Troubleshooting

| Symptom | Check first |
| --- | --- |
| Bot does not start | Python version, installed `requirements.txt`, `DISCORD_TOKEN`, and root `main.py` startup command |
| A slash command is missing | Confirm bot has `applications.commands` scope, wait briefly for command registration, then restart once |
| `/setup-workflow` cannot create resources | Bot role must have Manage Channels, Manage Roles, and Manage Threads, with suitable role position |
| Google verification fails | Service account key path, all three enabled Google APIs, and service-account sharing on the Sheet, template, and Shared Drive folder |
| Drive reports storage quota exceeded | Use a Shared Drive and give the service account proper membership; service accounts should not be used as personal Drive storage |
| Form response has not appeared | Verify the correct response Sheet ID/tab name, wait 60–120 seconds, or use `/court-order-sync` as an administrator |
| Ticket details are incomplete or stale | An authorized administrator, Judge, or Attorney General can use `/refresh-court-order` in the active ticket to reload the original Form response and repost the current details |
| New Docket filing reports Missing Access | In the Docket Forum's permissions, allow the bot role to View Channel, Send Messages, and Create Public Threads; retain Manage Threads for later docket administration, then run `/court-order-sync` to retry the same response |
| Court Order must join a prosecuted case | In the private Court Order ticket, use `/merge-court-order-to-docket` and paste the destination Docket Forum post link or ID. Select **keep open** for continued investigative work, or **close** after the copy to create a permanent staff record and remove the source ticket |
| A prosecutor, Judge, defense attorney, or PD officer changes | Use `/assign-case` for a direct Command/admin reassignment of the selected Slot 1 or Slot 2, or let the current assignee offer `/transfer-case-assignment`; the recipient must accept the same slot in the ticket with `/accept-case-transfer` |
| Requester cannot claim | The Discord username submitted on the Form must match their current server username; correct and resubmit if it does not |
| Warrant generation fails | Check template placeholders, Google Docs API access, document sharing, three-subject/character limits, and one-page layout |
| FiveManage URL is absent | Treat it as optional; verify the protected token and storage path while confirming the Discord PNG was still created |
| Changes pushed to GitHub do not appear | Confirm Wispbyte synchronized the selected branch, then restart the server |

## Before enabling a live server

Repeat the entire checklist in an isolated test environment. Then review every
live role, category, channel, Form, response Sheet, template, Shared Drive
permission, Wispbyte protected variable, and backup process. Configure live
IDs in a dedicated validated production profile; do not point a test profile at
live Discord or Google resources.

This guide is maintained alongside the bot. Whenever a new workflow, command,
host requirement, or required permission is added, update this file and the
main README in the same change.
