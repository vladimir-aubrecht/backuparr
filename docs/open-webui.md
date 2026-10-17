# Open WebUI API backup and recovery

This integration exports recoverable application data through Open WebUI's native
HTTP API. It does **not** create a server/database snapshot. Both SQLite and
PostgreSQL installations can use this API export.

The API contracts were checked against Open WebUI **0.12.3**, commit
[`91ea4e3`](https://github.com/open-webui/open-webui/tree/91ea4e3421d1d8069f13d4a463fcb5986d94ea60).
Restore first to exactly the exported version, then upgrade. Older releases can
have different export schemas; unsupported responses stop the export instead of
silently producing an incomplete archive. No live Open WebUI instance was used
for the automated contract tests.

## Configure

Set the server root URL (for example `http://open-webui:8080`) and an administrator's
API key in Backuparr. Enable API keys in Open WebUI and create the key in the
administrator's Settings > Account. Backuparr stores it using its normal encrypted
credential storage. A normal user's token is rejected for backup.

`ENABLE_ADMIN_EXPORT` must be enabled for the all-users chat export. If API-key
endpoint restrictions are enabled, allow the relevant `/api/v1/` routes in the
table below. `BYPASS_ADMIN_ACCESS_CONTROL` controls whether workspace/file exports
include other users' private resources. **Without it, workspace coverage is limited
to the administrator's accessible resources**, even though the chat export includes
all accounts. The archive manifest records this coverage limitation.

Schedule exports when the server is idle: individual API reads are not an atomic
snapshot. Permission failures, inaccessible original uploads, inconsistent
pagination and incomplete file downloads fail the export. Failed runs do not
publish a partial archive. Progress is logged per exported section and file.

## Included and how it is recovered

| Data | Export | Recovery |
| --- | --- | --- |
| All users' chat content, message history, tags, timestamps, pin/archive state | `GET /api/v1/chats/all/db` | Per-user `POST /api/v1/chats/import`; new chat IDs |
| Per-user settings | Paginated `GET /api/v1/users/` | Per-user `POST /api/v1/users/user/settings/update` |
| Persistent application settings, including provider credentials (excluding MFA policy) | `GET /api/v1/configs/export` | Admin `POST /api/v1/configs/import` |
| Accessible custom model definitions | `GET /api/v1/models/export` | Owner's `POST /api/v1/models/import` |
| Accessible prompts, including disabled prompts | Paginated `GET /api/v1/prompts/list` | Owner's `POST /api/v1/prompts/create`, followed by toggle for disabled prompts |
| Accessible custom tools and global tool valves | `GET /api/v1/tools/export`, `GET /api/v1/tools/id/{id}/valves` | Owner's create and valves/update endpoints |
| Functions, global valves, activation/global flags | `GET /api/v1/functions/export`, `GET /api/v1/functions/id/{id}/valves` | Admin `POST /api/v1/functions/sync` into an empty function workspace |
| Accessible original uploaded files | Paginated `GET /api/v1/files/`, `GET /api/v1/files/{id}/content` | Multipart `POST /api/v1/files/`; new IDs are mapped into restored content |
| Accessible local knowledge bases and document membership | Paginated `GET /api/v1/knowledge/` and `/{id}/files` | Create knowledge bases, upload documents and add files; target reindexes them |

The archive includes a minimal user directory to match each resource to its owner.
It does **not** contain the users' authentication secrets and does not recreate
accounts. Use each corresponding user's token during recovery; an administrator
token cannot impersonate other owners through the native chat import API.

References in chat JSON are user-controlled and are **not proof of permission** to
read a file or knowledge base. The CLI therefore refuses to copy foreign-owned
resources by default. If you have independently verified the source permissions,
`--include-shared-resources` permits private copies for the restoring user; the
preview lists their original owners and identifiers. Do not enable this flag just
to suppress an error. Source ACL/group membership is not automatically verified. File references
missing from the admin listing are fetched individually using the admin API;
unavailable internal uploads fail the export. External links remain references.

Global functions become owned by the restoring administrator. User resources become
owned by the matching target account. Sharing grants are deliberately recreated
as private: source user/group IDs must not grant access to unrelated target users.
Recreate sharing after all users have restored their data. Admin visibility limits
still apply to exports; this is not a bypass of Open WebUI's access controls.

## Excluded

- Database files, passwords/password hashes, API keys, MFA, OAuth/SSO state,
  account creation, groups and sharing permissions. The three persistent MFA policy keys are excluded from importable configuration because changing them requires an interactive JWT admin session, not an API key. Recreate accounts with the
  same email addresses and set passwords/SSO manually.
- Model weights, external provider state, runtime environment variables,
  container configuration, server secrets, webhooks beyond persistent settings,
  generated/cache assets and resources outside the native API.
- Vector database and embeddings. Uploaded knowledge documents are restored and
  reindexed using a working target embedding provider; original file bytes must
  be available. External knowledge connectors' remote data is not backed up.
- Knowledge directory hierarchy, chat folder hierarchy, public share links,
  workspace version history, per-user tool/function valves (source-server ciphertext is removed from user settings), channels,
  evaluations/feedback and account profile/status metadata.
- Referenced external URLs are not downloaded. Generated images/audio or other
  non-upload cache URLs in messages are references, **not backed-up assets**.
  Likewise a model preset is not the underlying model/provider installation.

The native folder listing (`GET /api/v1/folders/`) is scoped to the authenticated
account (`Folders.get_folders_by_user_id(user.id)`), so an administrator cannot
enumerate every user's private folder tree with the one configured token. Chat
content remains exportable through the separate all-users chat endpoint; recover
folder organization manually. The same one-token constraint prevents exporting
other users' decrypted per-user tool/function valves.

Linked notes, attached chats/folders, filesystem attachments and ephemeral web/text
retrieval collections are not recreated. Their historical conversation text remains
in `chats.json`, but the CLI refuses dependent conversations before any writes;
recover their content using Open WebUI's native chat import and recreate these linked
resources manually. Filter `chats.json` by the matching `users.json` user's `id`
before any manual import: the export contains **every account's** conversations.
The API body is `{"chats": [...]}`; each entry keeps `chat`, `meta`, `variables`,
`pinned`, `archived`, `current_message_id`, `created_at` and `updated_at` from that
user's exported records. Authenticate as that user. To recover history without the
unsupported attachments, remove those specific linked objects from the copied chat
payload after reviewing them; leave the original archive intact. These attachment IDs are not treated as uploaded-file IDs.

Some export objects contain reference metadata for excluded items. Those references
are not a promise that the excluded data can be restored. Archives contain private
conversations and secrets: store them with appropriate access controls.

### Why not download `webui.db`?

Open WebUI's [`GET /api/v1/utils/db/download`](https://github.com/open-webui/open-webui/blob/91ea4e3421d1d8069f13d4a463fcb5986d94ea60/backend/open_webui/routers/utils.py#L74)
streams the live SQLite database file without a SQLite backup operation and without
its WAL. [`DATABASE_ENABLE_SQLITE_WAL` defaults to true](https://github.com/open-webui/open-webui/blob/91ea4e3421d1d8069f13d4a463fcb5986d94ea60/backend/open_webui/env.py#L330).
Downloading that file can miss committed changes. This integration therefore never
presents the raw download as a complete backup. A complete disaster-recovery backup
needs a consistent database snapshot plus uploaded/server files outside this native
API integration.

## Restore

The Restore tab is disabled for this integration because recovery needs separate
user credentials and preparation. Use the bundled CLI inside Backuparr or from a
checkout with the normal dependencies installed. It previews by default.

1. Prepare a fresh Open WebUI installation on **the same version** as the archive.
   Configure working model/embedding providers. Create the same user accounts with
   their original email addresses and enable API keys/import/workspace permissions
   for the users who will recover their data. Keep the target private during recovery.
2. Make the archive and each target user's API key file available to the CLI. Key
   files should be readable only by the operator (`chmod 600`). Do not put a key in
   command-line arguments or logs.
3. Restore functions with the target administrator's token if applicable. This
   requires an empty function workspace and preserves activation/global flags.
   Functions/tools execute Python on the target: use only trusted archives.
4. Restore each user's resources with **that user's own token**. Accounts must have
   empty chats and workspace resources. The CLI checks for visible ID collisions,
   imports files and knowledge first, then tools/models/prompts/chats/settings. Foreign-owned references require manually verified source permissions and the explicit `--include-shared-resources` option.
5. Recreate sharing permissions and folders manually. Review external references,
   model availability and document indexing.
6. Optionally import server configuration **last**, with an admin token. It can
   change providers, authentication settings and permitted endpoints. Review the
   archived `config.json`, ensure its secrets/endpoints are appropriate for the
   target and restart Open WebUI after importing if required.

```bash
# Preview functions (admin token); add --apply after reviewing the summary.
python -m apps.open_webui_restore export.zip \
  --url http://open-webui:8080 --api-key-file /run/secrets/admin-key \
  --component functions

# Preview Alice's own resources, authenticating as Alice on the target.
python -m apps.open_webui_restore export.zip \
  --url http://open-webui:8080 --api-key-file /run/secrets/alice-key \
  --source-user alice@example.com

# Perform the same recovery after reviewing the preview.
python -m apps.open_webui_restore export.zip \
  --url http://open-webui:8080 --api-key-file /run/secrets/alice-key \
  --source-user alice@example.com --apply

# Server settings last; this is an explicit separate operation.
python -m apps.open_webui_restore export.zip \
  --url http://open-webui:8080 --api-key-file /run/secrets/admin-key \
  --component config --apply
```

Restore is not transactional. If any step fails, stop and inspect the partially
restored target; do not retry into it blindly. The CLI refuses nonempty target
workspaces, rather than overwriting or duplicating existing conversations. Keep the
original archive until recovery has been independently verified.

## API source references

All links are pinned to the reviewed upstream commit:

- [Chat exports and imports](https://github.com/open-webui/open-webui/blob/91ea4e3421d1d8069f13d4a463fcb5986d94ea60/backend/open_webui/routers/chats.py)
- [Chat import ownership](https://github.com/open-webui/open-webui/blob/91ea4e3421d1d8069f13d4a463fcb5986d94ea60/backend/open_webui/models/chats.py)
- [Files: pagination, original downloads and multipart upload](https://github.com/open-webui/open-webui/blob/91ea4e3421d1d8069f13d4a463fcb5986d94ea60/backend/open_webui/routers/files.py)
- [Knowledge membership and import](https://github.com/open-webui/open-webui/blob/91ea4e3421d1d8069f13d4a463fcb5986d94ea60/backend/open_webui/routers/knowledge.py)
- [Configuration export/import](https://github.com/open-webui/open-webui/blob/91ea4e3421d1d8069f13d4a463fcb5986d94ea60/backend/open_webui/routers/configs.py)
- [Model export/import](https://github.com/open-webui/open-webui/blob/91ea4e3421d1d8069f13d4a463fcb5986d94ea60/backend/open_webui/routers/models.py)
- [Prompts](https://github.com/open-webui/open-webui/blob/91ea4e3421d1d8069f13d4a463fcb5986d94ea60/backend/open_webui/routers/prompts.py), [tools](https://github.com/open-webui/open-webui/blob/91ea4e3421d1d8069f13d4a463fcb5986d94ea60/backend/open_webui/routers/tools.py), [functions](https://github.com/open-webui/open-webui/blob/91ea4e3421d1d8069f13d4a463fcb5986d94ea60/backend/open_webui/routers/functions.py)
- [API key authorization/restrictions](https://github.com/open-webui/open-webui/blob/91ea4e3421d1d8069f13d4a463fcb5986d94ea60/backend/open_webui/utils/auth.py)
