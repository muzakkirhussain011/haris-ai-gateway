# Google Drive

Copies who may read each Drive file onto the file's chunks, so that
[`MetadataACLAuthorizer`](../concepts.md) filters searches the way Drive's own sharing does. Call
it at ingestion, and again whenever a file's sharing changes. No extra dependency: you pass the
Drive service from Google's client, which you install yourself.

```bash
pip install wardowl google-api-python-client google-auth
```

```python
from google.oauth2 import service_account
from googleapiclient.discovery import build

from wardowl.sync import with_access
from wardowl.sync.google_drive import GoogleDriveACL

credentials = service_account.Credentials.from_service_account_file(
    "sync-account.json",
    scopes=["https://www.googleapis.com/auth/drive.readonly"],
    subject="indexer@example.com",  # domain-wide delegation: the account your ingestion reads files as
)
drive = GoogleDriveACL(build("drive", "v3", credentials=credentials), domains=["example.com"])

access = drive.access(file_id)  # who may read the file
backend.set_metadata({cid: with_access(metadata, access) for cid, metadata in chunks.items()})
```

`chunks` maps the file's chunk ids to the metadata you stored with them. `with_access` replaces
`allowed_users` and `allowed_groups` and keeps every other key, and `set_metadata` stores the
result without embedding the chunks again
([Changing a document's permissions](../operations.md#changing-a-documents-permissions)).

## How Drive sharing maps onto subjects

| Drive grant | Gives | A `Subject` matches when |
|---|---|---|
| A user, any role | `allowed_users`: the address, lowercased | its id is that address, lowercased |
| A group, any role | `allowed_groups`: the address, lowercased | its `groups` list the group |
| A domain, findable in search | `allowed_groups`: `"domain:example.com"` | its `groups` list `"domain:example.com"` |
| Anyone, findable in search | `allowed_groups`: `"*"`, or with `domains` each `"domain:..."` | always, or in those domains |

Every Drive role (reader, commenter, writer, file organizer, organizer and owner) can read the
file's content, so each one counts. Use the same addresses as subject ids, and give each subject
the `domain:` group of its own domain.

Drive groups can contain groups, and a grant to a group reaches the members of the groups inside
it. A subject's `groups` must therefore list every group it belongs to, directly or not. Cloud
Identity's
[`searchTransitiveGroups`](https://docs.cloud.google.com/identity/docs/reference/rest/v1/groups.memberships/searchTransitiveGroups)
returns them in one call (Workspace Enterprise and Cloud Identity Premium); otherwise call the
Admin SDK's `groups.list(userKey=...)` for the user and again for each group found. Resolve them
when the user signs in, as for any group-based permission: the chunks need no update when someone
joins or leaves a group.

## What grants nothing

A grant is skipped when it does not let its holder open the file's content, or when it cannot be
read with certainty. Skipping fails closed: the file is readable by fewer people, never more.

- **Sharing by link alone.** "Anyone with the link" and "anyone in example.com with the link" let
  in whoever holds the link. A search across every document would show the content to people who
  never had the link, so these grants count only with `GoogleDriveACL(service, link_sharing=True)`.
  Grants that are also findable in search (`allowFileDiscovery`) always count.
- **Deleted accounts.** A deleted account's address can later be given to someone else.
- **Grants with an expiry time**, unless you pass `expiring_grants=True`; see
  [grants that expire](#grants-that-expire).
- **Expired grants**, and grants whose expiry time cannot be read, in every case.
- **Grants that belong to a view.** On a limited-access folder, a `metadata` view grant shows the
  folder's details, not its content; `published` grants are left out as well.
- **Inherited grants on a limited-access item.** Only owners, organizers and people added to the
  item directly can open it.
- **Grants to non-members** in a shared drive open only to its members.
- **Grants outside your domains**, when you pass `domains`; see below.
- **Unknown types and roles**, and grants missing the address or domain they need.

## Grants that expire

Drive lets a share end at a set time (`expirationTime`), and removes the grant itself when it
does, which your sync may never be told about. Copied onto the chunks, such a grant would keep
granting access after Drive stopped. So by default a grant with an expiry time counts for nothing,
even before it expires.

`GoogleDriveACL(service, expiring_grants=True)` counts it until it expires. The answer's
`expires_at` is then the earliest expiry among the grants that count, and `with_access` writes it
to the chunks as `access_expires_at`, in UTC to the second (`"2026-10-17T12:00:00Z"`).
`MetadataACLAuthorizer` does not read that field: sync the file again by that time, for example
from a job that looks for chunks whose `access_expires_at` has passed, or enforce it in a policy.
A later sync without expiring grants removes the field.

```python
drive = GoogleDriveACL(service, domains=["example.com"], expiring_grants=True)
access = drive.access(file_id)
if access.expires_at is not None:
    schedule_sync(file_id, at=access.expires_at)  # your scheduler
```

## Shared drives and your domains

A shared drive can be open only to its members, or only to users of its domain. Grants made
before the setting changed can still be listed, but they no longer open the file, so
`GoogleDriveACL` reads each file's shared drive settings (`drives.get`) and applies them: in a
members-only drive, only grants that come from drive membership count.

`domains=["example.com"]` lists your organization's domains. Grants to addresses or domains
outside them then count for nothing, and an "anyone" grant gives each of your domains' groups
instead of everyone. Pass it when sharing outside the organization is turned off, when your
subjects only ever come from your own domains, and always for shared drives open only to their
domain's users: a file in such a drive raises `ValueError` without it. A subdomain counts only
when it is listed.

## Credentials

The reader needs the `drive.readonly` scope: `drives.get`, which reads a shared drive's settings,
accepts no narrower one. Use the identity your ingestion already reads the files with, for
example a service account with
[domain-wide delegation](https://support.google.com/a/answer/162106) acting as an indexing user
who can see each file's sharing and is a member of each shared drive you index. An administrator
does not see other users' files or shared drives they are not a member of, so acting as one is
not enough.

A file that is gone for this identity, because it was deleted or is no longer shared with it
(HTTP 404, or 403 with the reason `notFound` or `insufficientFilePermissions`), is readable by
nobody: `access` returns `Access()`, and writing that to the chunks revokes every grant. An
identity that cannot see a file's sharing therefore hides the file from every search, so check
that it can. Any other error from the API, such as a rate limit (429) or a server error (5xx), is
raised: a file whose sharing could not be read keeps the permissions it had until a later sync
succeeds.

## Keeping permissions current

Drive's [changes feed](https://developers.google.com/workspace/drive/api/guides/manage-changes)
reports every file whose content or sharing changed since a saved page token. Read it on a
schedule and sync each changed file you index:

```python
changes = build("drive", "v3", credentials=credentials).changes()
token = load_token() or changes.getStartPageToken(supportsAllDrives=True).execute()["startPageToken"]
fields = "nextPageToken,newStartPageToken,changes(fileId,removed)"
while token:
    request = changes.list(pageToken=token, includeItemsFromAllDrives=True, supportsAllDrives=True, fields=fields)
    page = request.execute()
    for change in page.get("changes", []):
        if change.get("removed"):
            backend.delete(chunk_ids(change["fileId"]))
        elif is_indexed(change["fileId"]):
            access = drive.access(change["fileId"])
            backend.set_metadata({cid: with_access(md, access) for cid, md in chunks(change["fileId"]).items()})
    token = page.get("nextPageToken")
    if "newStartPageToken" in page:
        save_token(page["newStartPageToken"])
```

A removed change (`removed`) means the file was deleted or the identity lost access to it: delete
its chunks. If a file is deleted between a change and its sync, `drive.access` returns `Access()`,
so its chunks are readable by nobody until the feed reports the removal.

Sharing a folder changes who may read every file inside it, and the feed lists those files. Save
the token only after the files are synced, so a crash repeats the work rather than skipping it.
Until a change is synced, the chunks keep their old permissions; sync often enough that this
window is acceptable, and see [Operations](../operations.md#changing-a-documents-permissions)
for what changes take effect at once.

## One writer at a time for each file

Two writers usually write a file's chunks: your indexer, with `add`, and the permission sync above,
with `set_metadata`. Every backend keeps the write that lands last, whenever its data was read. An
indexer that read a file's sharing before a revocation, and writes after the sync wrote the
revocation, puts the revoked access back:

| Step | Indexer | Permission sync | The chunks |
|---|---|---|---|
| 1 | Reads the file and its sharing: alice may read it | | alice may read |
| 2 | | Drive removes alice, and the feed reports the file | alice may read |
| 3 | | Reads the sharing and writes it with `set_metadata` | alice may not read |
| 4 | Writes the chunks it read in step 1 with `add` | | alice may read again |

The feed reported the change once, in step 2, so nothing corrects step 4 until the file changes
again. Run the writes for one file one after another, each from its read to its write: put a file's
indexing and its syncs in one queue or worker chosen by the file id, or hold a lock on the file id
from the read to the write in both. Writes for different files can still run at the same time.

### Revisions, on pgvector and MongoDB

`PgVectorBackend` and `MongoDBBackend` can also refuse a write older than what is stored. Pass the
file's revision to both `add` and `set_metadata`: a chunk stored with a newer revision is then not
replaced, and the call raises `StaleRevision` ([pgvector](pgvector.md#revisions),
[MongoDB](mongodb.md#revisions)). In step 4 above, the indexer's write would be refused.

```python
from wardowl.backends import StaleRevision

files = build("drive", "v3", credentials=credentials).files()
# Read the revision first: before the sharing and, in the indexer, before the content.
version = int(files.get(fileId=file_id, fields="version", supportsAllDrives=True).execute()["version"])
access = drive.access(file_id)
try:
    backend.set_metadata({cid: with_access(md, access) for cid, md in chunks(file_id).items()}, revision=version)
except StaleRevision:
    pass  # a newer write is stored already
```

- Read the revision before the data you write with it, never after. A revision read after the
  data can be newer than the data, and an old answer would then be stored under a new number.
- `StaleRevision` means a newer write is stored already. The sync has nothing more to do; the
  indexer reads the file again and writes what it says now, under its new revision.
- Google documents Drive's `version` as a number that only grows and that reflects every change to
  the file on the server. It does not say whether a change of sharing counts, and wardowl's tests
  do not run against Drive. Before relying on it, change a test file's sharing and check that its
  `version` grows: if it does not, an old answer written under the same version is not refused.
- The comparison is per chunk. A chunk that is not stored has no revision, so it is written: while
  a file's chunks are deleted and added again, or when its chunk ids change from one version to the
  next, a late write is not caught. Keep ids such as `"<file id>#<n>"`, and keep the writes for
  each file in order; revisions catch what slips past that.
- Chroma and the other backends have no revision check. With them, running the writes for each
  file one after another is the only protection.

## Checking the mapping

The tests run the reader against the response shapes Google documents for `permissions.list`,
`files.get` and `drives.get`, not against a live Workspace domain. Before relying on it, compare
its answer with Drive's sharing dialog for a few files of each kind you index: My Drive, shared
drives, limited-access folders and files shared by link.

`drive_access` is the mapping alone, from the permission resources Drive returns to an `Access`.
Use it to test your own data, or with permissions you already fetched:

```python
from wardowl.sync.google_drive import drive_access

permissions = [{"type": "group", "role": "reader", "emailAddress": "eng@example.com"}]
assert drive_access(permissions).groups == {"eng@example.com"}
```
