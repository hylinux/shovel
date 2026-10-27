# Local Outlook resources

## Supported environment

The `outlook_local` connector reads classic Outlook through the Outlook Object
Model, in the logged-in Windows user's interactive desktop session. Outlook must
have a configured profile. Install Shovel's `outlook` extra for pywin32.

The committed Zvec version provides a Windows x64 Python 3.14 wheel. Use x64
Python, including under emulation on Windows ARM64:

```powershell
uv python install 3.14-x86_64
uv sync --python 3.14-x86_64 --locked --all-extras
uv run shovel run dev
```

New Outlook, Windows services/SYSTEM accounts, non-Windows COM access, and direct
PST/OST file parsing are outside this connector. Archives already mounted in
classic Outlook are accessible through their Outlook StoreID.

## Resource configuration

| Field | Default | Meaning |
| --- | --- | --- |
| `store_id` | Required | Outlook StoreID; never inferred from a display name |
| `folder_ids` | Required | 1-100 explicitly authorized FolderIDs within that store |
| `include_subfolders` | `false` | Recursively enumerate selected folders |
| `timeout_seconds` | `60` | Per-worker-request timeout, 5-300 seconds |
| `max_body_chars` | `1000000` | Body text cap, 1,000-4,000,000 characters; truncation is visible |

Use the workbench's mailbox/folder browser or:

```powershell
uv run shovel resource outlook-stores
uv run shovel resource outlook-folders --store-id "<StoreID>"
uv run shovel resource outlook-folders --store-id "<StoreID>" --parent-id "<FolderID>"
```

The REST equivalents are `GET /api/v1/outlook/stores` and
`GET /api/v1/outlook/folders?store_id=...&parent_id=...`. Omit `parent_id` to list
the store root's children. These routes use the same local-only access checks as
the rest of the workbench. Discovery errors return an explicit 503, not an empty
mailbox list. Selecting a resource does not automatically scan mail.

Scan profiles remain shared with other resources. `include`/`exclude` use
case-sensitive glob matching against `FolderPath\Subject`; `target_ids` contain
the opaque external IDs, not subjects or file paths. `max_file_bytes` checks
Outlook's complete message size (including attachments). `time_window_days`
filters received time, not modification time. The incremental `since` hint is
deliberately ignored: all in-scope metadata is enumerated so older arrivals and
edits are not excluded by a timestamp cursor. No WATCH capability is advertised.

## Execution and lifecycle

```text
Host operation -> ScanService -> OutlookConnector -> private stdio subprocess
                                                 -> classic Outlook COM (STA)
             <- normalized mail -> parser -> chunks -> SQLite / optional Zvec
```

The worker is shipped inside the Python package and launched only when needed.
It exposes no network listener, accepts a fixed command set, and never opens
Shovel's database or vector index. All COM objects stay on its STA thread.
Verification and mailbox/folder browsing use short-lived workers. A scan reuses
one worker for discovery and per-message reads; normal completion, failure,
timeout and cancellation close it. A blocked worker is terminated after a bounded
grace period; Shovel does not call Outlook's `Quit` or terminate Outlook itself.

Discovery uses Outlook tables in batches of 100. Only `IPM.Note` and its subclasses
are indexed; appointments, contacts, and meeting requests are not documents.
Folder IDs are deduplicated when recursive selections overlap.

The worker permits reads only for messages discovered in the configured folders.
It checks the current folder/store and the revision before reading, and rechecks
the revision afterward. A mail move/edit during scanning is reported as a
per-document failure and can be retried by another ordinary scan.

## Representation and indexing

Each message is one Document. Source identity is a SHA-256 identifier derived from
StoreID plus EntryID; its URI is `outlook-local://mail/<identifier>`. It is an
internal citation identifier, not an Outlook launch URL. Subjects and addresses
are not embedded in source identifiers.

The source revision fingerprint includes EntryID, sub-second last-modified time,
and size. This is a conservative change fingerprint, not a cryptographic
hash of all mail contents. Some non-content Outlook changes can cause reprocessing;
`--force` is available for a complete reread.

The worker returns versioned `outlook-mail-v1` JSON, not original MIME. The parser
is selected by an explicit content-format hint, so a subject such as `budget.pdf`
cannot accidentally invoke the PDF parser. This does not add arbitrary `.eml` or
`.msg` file parsing to the filesystem connector.

Headers and body form the indexed text. Subject, sender, and received time provide
context for small-to-big chunking. Sender, recipients, dates, folder, thread ID and
attachment names/sizes are also retained in `source_meta`; search and persisted
chat citations expose relevant mail metadata. Sender/recipient strings come from
Outlook's display/address properties; Exchange legacy addresses are not silently
represented as resolved SMTP addresses. No address-book resolution is performed.

Existing chunking, vector completion, recovery, model-version invalidation,
`--no-embed` backfill, and deletion-outbox cleanup apply unchanged. Parent chunks
are not embedded. Metadata is not a new structured mail-query API: exact sender
and date-range search filters are not implemented.

## Privacy and limitations

The integration reads text bodies, without changing mail or downloading
attachments. Header-only messages must be fully downloaded in Outlook first.
No readable text is an error, never a successful empty index. Reading Outlook may
still invoke provider/network activity; this is not a guarantee of offline access.
Plain-text conversion is supplied by Outlook; HTML is not executed and external
images are not loaded by Shovel.

Reply history and signatures are retained, not heuristically stripped. Attachment
contents, automatic threading, arbitrary source opening, and live notifications
are not implemented. Store moves can change EntryID and are treated as new
sources; Internet Message-ID is not used to silently merge copies.

The connector does not advertise DELETE_DETECT. Missing/deleted/moved mail may
remain searchable in Shovel, even after a successful full sweep. Deleting a Shovel
resource removes its indexed mail through existing durable cleanup, never the
source mailbox. Changing resource configuration invalidates the old source scope
through existing resource-service behavior.

Indexed mail and persisted citation excerpts are sensitive local data. Remote
embedding/chat providers receive those excerpts if configured. There is no
additional plaintext export archive or mailbox credential store; access uses the
current Outlook profile and does not bypass organization policies.

## Validation

`backend/tests/test_outlook.py` exercises protocol failures, worker cleanup,
authorization/revision checks, discovery filters, mail parsing, API origin checks,
keyword indexing, vector backfill and deletion-detection limits without requiring
an Outlook profile. Existing parser/chunker/pipeline tests cover shared behavior.

Real COM acceptance requires a Windows desktop with classic Outlook and explicitly
selected test folders. Verify cached text/HTML mail, Chinese subjects, PST folders,
security prompts, changed/moved mail, header-only mail, cancellation and repeated
scans. Automated fake-COM tests do not establish Office-version compatibility.
