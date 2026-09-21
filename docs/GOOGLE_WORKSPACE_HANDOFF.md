# Google Workspace handoff guide

This guide makes the Google side of NERP - Case Management portable. A recipient
does not reuse your Google account, your service-account JSON credential, or your
existing file IDs. They create their own Google resources, share those resources with
their own service account, and place the resulting IDs in their private
`config/master.yaml`.

## What to hand over

Provide the recipient access to a single Google Drive folder named **NERP - Case
Management Handoff**. Put these items in that folder:

1. The Case Management System Google Form.
2. Its linked Google Sheets response workbook.
3. The Arrest Warrant, Search / Seizure Warrant, and Subpoena Google Docs templates.
4. Any approved image assets used by the templates.
5. This guide and the package copy of `docs/` from the deployment repository.

Do not include generated case documents, response data, evidence, a service-account
JSON key, or any populated deployment configuration in the handoff folder.

## Recipient setup order

1. Create a Shared Drive or a shared folder owned by the recipient organization.
2. Copy the three document templates into a `Court Order Templates` subfolder.
3. Copy the Case Management System Form. In the copied Form, create a new linked
   response Sheet owned by the recipient; do not use the source organization's Sheet.
4. Create an output subfolder such as `Generated Court Orders` for bot-created
   Google Docs and internal PDF validation artifacts.
5. In the recipient's Google Cloud project, create a service account and enable the
   Google Drive, Google Docs, Google Sheets, and Google Forms APIs.
6. Download the service-account JSON only to a protected folder on the host that will
   run the bot. Do not upload it to Drive or GitHub.
7. Share the template folder, output folder, and response workbook with the service
   account email. Grant the least access that still permits the bot to read the Form
   responses, copy templates, fill copies, export documents, and create tracker files.
8. In the private deployment repository, fill the `environment.google_workspace`,
   `court_order_intake`, and each `court_order_*` section of `config/master.yaml`
   using the recipient-owned resource IDs.
9. Put only the JSON file's absolute host path into
   `runtime.google_service_account_file`. Example paths are shown in
   [CONFIGURATION.md](../CONFIGURATION.md).
10. Start the bot and run `/google-workspace-status` as a configured bot
    administrator. It confirms access and creates or finds the shared request tracker.

## Preserving template compatibility

Keep placeholders as literal text in ordinary Google Docs table cells; do not place
them in drawings or text boxes. The current document workflows use these placeholders:

| Document | Required placeholders |
| --- | --- |
| Arrest Warrant | `{{REQUEST_ID}}`, `{{DOCKET_ID}}`, `{{CLASSIFIED}}`, `{{REQUESTER_NAME}}`, `{{REQUESTING_AGENCY}}`, `{{PCO}}`, `{{CASE_OFFICER_AGENCY}}`, `{{SUBJECT_1_NAME}}` through `{{SUBJECT_3_NAME}}`, `{{S1ID}}` through `{{S3ID}}`, `{{SUBJECT_1_INITIAL_CHARGES}}` through `{{SUBJECT_3_INITIAL_CHARGES}}`, `{{PROBABLE_CAUSE}}`, `{{APPROVER_NAME}}`, `{{ISSUE_DATE}}` |
| Search / Seizure Warrant | The common request placeholders above, `{{SUBJECT_1_NAME}}` through `{{SUBJECT_3_NAME}}`, `{{S1ID}}` through `{{S3ID}}`, `{{SUBJECT_1_DATE}}` through `{{SUBJECT_3_DATE}}`, `{{SUBJECT_1_SEARCH_SEIZURE_TYPE}}` through `{{SUBJECT_3_SEARCH_SEIZURE_TYPE}}`, `{{PROBABLE_CAUSE}}`, `{{APPROVER_NAME}}`, `{{ISSUE_DATE}}` |
| Subpoena | The common request placeholders above, `{{SUBJECT_1_NAME}}` through `{{SUBJECT_3_NAME}}`, `{{S1ID}}` through `{{S3ID}}`, `{{SUBJECT_1_DATE}}` through `{{SUBJECT_3_DATE}}`, `{{SUBJECT_1_SUBPOENA_TYPE}}` through `{{SUBJECT_3_SUBPOENA_TYPE}}`, `{{SUBPOENA_DETAILS}}`, `{{APPROVER_NAME}}`, `{{ISSUE_DATE}}` |

The player-facing output must remain one page. Keep no more than three subjects in
these templates and preserve the reserved layout space for the approval block.

## Values to record in the private master configuration

| `config/master.yaml` value | Where the recipient gets it |
| --- | --- |
| `google_workspace.drive_folder_id` | Shared Drive or shared-folder ID for the request tracker |
| `court_order_intake.form_url` | Copied Case Management System Form URL |
| `court_order_intake.response_spreadsheet_id` | ID of the Form's newly linked response Sheet |
| `court_order_intake.response_sheet_name` | Visible response-tab name in that workbook |
| Each `template_document_id` | ID from the copied Google Docs template URL |
| Each `output_drive_folder_id` | ID of the recipient-owned generated-output folder |

## Moving from this organization to another

Copy the assets first, confirm that the recipient owns the copies, then update the
recipient's private master configuration. The source organization should retain its
existing Drive content and service account until the recipient has successfully
completed the `/google-workspace-status` check and one complete document workflow.
Never make a source Google asset depend on a recipient's service account or vice versa.
