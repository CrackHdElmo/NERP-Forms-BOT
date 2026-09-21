# NERP - Case Management

## Bot Staff and Player Reference Guide

This guide explains the current NERP - Case Management features, Discord commands, and
recommended workflows. It is written for players, DOJ/PD staff, judges, attorneys, and bot
administrators. The live Google Drive copy is intended for linking from `#service-desk`.

## Start here

`#service-desk` is the public starting point for court and case-management activity. Use its
form links to submit a request. The bot creates the appropriate private Court Order ticket or
Docket Forum post, where authorized participants can work the request.

- **Private Court Order ticket:** confidential requests, claimant verification, evidence,
  warrant/subpoena generation, and judicial review.
- **Docket Forum post:** active criminal or civil case record shared with the configured court
  staff roles.
- **`#doj-case-records`:** staff-only permanent closure records. Do not use it for new work.

Never share API tokens, service-account files, private evidence, or sensitive case information
in public channels.

## Player and requester workflow

1. Open `#service-desk` and select the correct form.
2. Enter your current Discord username accurately. For Court Orders, the bot uses it to verify
   the requester later.
3. Submit the form. The bot creates an ID such as `COR-000123` for a Court Order or
   `DCK-000123` for a new Docket.
4. For a Court Order, open the private ticket provided by the bot and use `/claim-court-order`.
   The submitted username must match your Discord username.
5. Upload any permitted documents, screenshots, media, and evidence in that private ticket.
6. Use the appropriate generation command only after the request has been claimed. A Judge or
   authorized administrator then reviews and approves or denies the request.

### Court Order document limits

Player-facing Arrest Warrants, Search / Seizure Warrants, and Subpoenas are generated as
single-page PNG files for FiveM use. Court Order requests support up to three subjects. The bot
will not silently trim information that cannot fit; it tells the requester when the form content
cannot safely produce a one-page document.

### Requester commands

| Command | Where to use it | Who may use it | What it does |
| --- | --- | --- | --- |
| `/claim-court-order` | The private Court Order ticket | The matching requester | Verifies ownership using the submitted Discord username and grants the requester access. |
| `/add-ticket-member` | A private bot-managed ticket | Verified requester or bot administrator | Adds one server member who can view, message, and attach files in that ticket. |
| `/generate-arrest-warrant` | Claimed Arrest Warrant ticket | Verified requester or bot administrator | Creates the proposed one-page Arrest Warrant PNG and internal document record. |
| `/generate-search-seizure-warrant` | Claimed Search / Seizure ticket | Verified requester or bot administrator | Creates the proposed one-page Search / Seizure Warrant PNG and internal document record. |
| `/generate-subpoena` | Claimed Subpoena ticket | Verified requester or bot administrator | Creates the proposed one-page Subpoena PNG and internal document record. |
| `/close-ticket` | The active ticket or Docket post | Requester, Judge, Attorney General, or administrator when authorized | Posts the required staff closure record, then closes the private ticket or archives/locks the Docket post. |

## Court Order and judicial commands

These commands must be used in the request’s active private ticket unless the table says the
command belongs in a Docket Forum post. A denial leaves a ticket open so a corrected Form can be
submitted or a later authorized reviewer can approve the request. The closure record retains the
full decision history.

| Command | Who may use it | What it does |
| --- | --- | --- |
| `/approve-arrest-warrant` | Configured Judge or bot administrator | Issues the final Arrest Warrant PNG with electronic `/s/ Name` approval and issue date. |
| `/deny-arrest-warrant` | Configured Judge or bot administrator | Denies an Arrest Warrant and records required denial notes. |
| `/approve-search-seizure-warrant` | Configured Judge or bot administrator | Issues the final Search / Seizure Warrant PNG with electronic approval. |
| `/deny-search-seizure-warrant` | Configured Judge or bot administrator | Denies a Search / Seizure Warrant and records required denial notes. |
| `/approve-subpoena` | Configured Judge or bot administrator | Issues the final Subpoena PNG with electronic approval. |
| `/deny-subpoena` | Configured Judge or bot administrator | Denies a Subpoena and records required denial notes. |
| `/refresh-court-order` | Configured administrator, Judge, or Attorney General | Reloads the original Google Form data and posts new intake/subject-detail embeds without changing the Court Order ID. |

## Docket, import, and merge commands

A **Court Order ID** has the format `COR-######`. A **Docket ID** has the format `DCK-######`.
Use these record IDs rather than Discord’s internal numeric channel IDs.

| Command | Where to use it | Who may use it | What it does |
| --- | --- | --- | --- |
| `/rename-docket` | Active Docket Forum post | Configured administrator, Judge, or Attorney General | Changes the visible Docket title and posts an audit notice. |
| `/merge-court-order-to-docket docket_id:DCK-######` | Source off-docket Court Order ticket | Configured administrator, Judge, or Attorney General | Immediately copies the Court Order into that Docket and keeps the original Court Order open. |
| `/merge-court-order-to-docket court_order_id:COR-######` | Destination Docket Forum post | Configured administrator, Judge, or Attorney General | Immediately copies that Court Order into the current Docket and keeps its source open. |
| `/import-court-order` | Destination Docket Forum post | Configured administrator, Judge, or Attorney General | Imports a `COR-######` Court Order and explicitly chooses whether its source remains open or closes. |
| `/close-ticket` | Active Docket Forum post | Authorized requester, Judge, Attorney General, or administrator | Creates the staff closure record and archives/locks the Docket post. |

When a new Docket Form includes valid `COR-######` Court Order references, the bot automatically
imports those Court Orders into the Docket while keeping the original private Court Order tickets
open.

## Case assignments and transfers

Every active case, Docket, and Court Order may record up to two people in each of these roles:
**Prosecutor**, **Judge**, **Defense Attorney**, and **PD Officer**. Empty positions appear as
**Unassigned**. A person must hold the corresponding Discord role before they can be assigned.

| Command | Who may use it | What it does |
| --- | --- | --- |
| `/assign-case` | Eligible staff assigning themselves; DOJ/PD Command or administrator assigning another person | Assigns or reassigns Slot 1 or Slot 2 for Prosecutor, Judge, Defense Attorney, or PD Officer. |
| `/transfer-case-assignment` | Current assignee, DOJ/PD Command, or administrator | Offers a selected role slot to another eligible staff member. The assignment does not change yet. |
| `/accept-case-transfer` | Named eligible transfer recipient | Accepts the pending transfer in the same ticket or Docket post and records the reassignment. |

## Service Desk administrator guide

`#service-desk` should remain read-only for regular members. It is the central, bot-managed
directory for forms, SOPs, policies, court resources, and approved external/community links.
Only configured bot administrators can use the `/service-desk` commands.

The bot maintains exactly one managed directory embed. Each edit updates that same embed instead
of creating duplicate posts. On first use, the bot creates it automatically.

| Command | What it does |
| --- | --- |
| `/service-desk edit` | Sets the embed title, optional introductory text, and/or direct HTTPS image URL. |
| `/service-desk form-link` | Adds or replaces one of four numbered form-link slots. Use clear labels such as `Case Management System` or `Attorney Request Form`. |
| `/service-desk external-link` | Adds or replaces one of four SOP, policy, Discord, or external-resource slots. |
| `/service-desk remove-link` | Removes a selected form or external-resource slot. |
| `/service-desk clear` | Removes the image or introductory text. |
| `/service-desk preview` | Shows the current embed privately before staff direct members to it. |
| `/service-desk adopt` | Uses the normal Discord message link to select an earlier **bot-authored** Service Desk post for future in-place updates. |

The bot cannot edit messages written by people or other apps. If an old manual directory post is
no longer wanted, a server administrator should archive or delete it manually, then use a
`/service-desk` command to create the maintained bot-owned directory.

### Recommended Service Desk order

1. **Case Management System** form
2. **File New Case / Docket** form or intake direction
3. **Attorney Request** form
4. **Business License / Corporate Office** form when active
5. **DOJ SOP and court rules**
6. **PD SOP / evidence handling policy**
7. **Court Docket** or court-calendar link
8. **Help / escalation contact**

Use short, descriptive link labels. Do not put classified evidence, personal identifying
information, case notes, credentials, or private staff-only URLs in the public Service Desk.

## Bot administration and maintenance

| Command | Who may use it | What it does |
| --- | --- | --- |
| `/bot-status` | Server members | Confirms that the bot is connected to the intended Discord server and environment. |
| `/setup-workflow` | Configured bot administrator | Guided setup for categories, channels, and Forums. It previews the planned resources before creating or registering them. |
| `/google-workspace-status` | Configured bot administrator | Confirms protected Google access and creates/fetches the test tracker when needed. |
| `/court-order-sync` | Configured bot administrator | Immediately imports unprocessed Case Management System responses. Normal polling runs automatically. |
| `/court-order-baseline` | Configured bot administrator | Marks existing Form responses as historical during initial setup so only later submissions become tickets. Use carefully. |
| `/bot-admin add` | Server owner or existing bot administrator | Gives a selected server member a durable, direct NERP bot-administrator grant. |
| `/bot-admin remove` | Server owner or existing bot administrator | Removes only that direct bot grant; it cannot remove server ownership, Discord Administrator permission, or a configured administrator role. |
| `/bot-admin list` | Server owner or existing bot administrator | Privately lists all direct NERP bot-administrator grants and who added them. |

### Bot administrator access

There are three independent ways a person can administer the bot: server ownership, Discord's
native **Administrator** permission, or a role listed under `roles.administrators` in the active
environment profile. `/bot-admin add` creates a fourth, direct grant for a named member. It is
useful when the person should manage the bot without receiving broad server-administrator power.

Only the server owner or someone who already has bot-administrator access can use the
`/bot-admin` command group. The direct grants are saved in the bot database and continue after a
restart. Use `/bot-admin remove` when that extra access should end; it affects only the direct
grant and deliberately cannot take away privileges supplied by Discord or a configured role.

See the root [configuration map](../CONFIGURATION.md) for where all deployment options live,
which values are protected secrets, and which options can be changed through Discord commands.

## Permissions and troubleshooting

- Bot administrators may be configured in the active environment, granted directly through
  `/bot-admin`, or be Discord server administrators/owners.
- Judges, Attorney General, DOJ/PD Command, prosecutors, defense attorneys, and PD Officers need
  the server roles configured for their corresponding workflows.
- The bot needs Discord permissions appropriate to its work: View Channel, Send Messages, Read
  Message History, Attach Files, Embed Links, Manage Roles for private-ticket access changes, and
  Manage Channels/Threads for setup, Docket management, or ticket closure.
- If a command does not appear after an update, verify that Wispbyte has restarted on the latest
  GitHub commit and allow Discord a short time to refresh guild commands.
- If a generated document fails, review the form limits, required template placeholders, Google
  sharing/API setup, and the Wispbyte console’s safe diagnostic message.
- If a Court Order or Docket ID cannot be found, verify the exact `COR-######` or `DCK-######`
  value before asking an administrator to investigate.

## Recordkeeping expectations

The bot preserves case history through request embeds, assignment records, approvals/denials,
evidence links, Docket imports, and the staff-only closure log. Use the correct request ticket or
Docket post for case work; do not move sensitive case content to public channels merely for
convenience.
