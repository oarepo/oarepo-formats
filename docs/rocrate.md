# RO-Crate import and export

This page is part of [oarepo-formats](index.md), which adds import and export formats to the records
of [oarepo-model](https://github.com/oarepo/oarepo-model) based Invenio repositories.

The **RO-Crate formats** (`oarepo_formats.rocrate`) let users download a record as an
[RO-Crate](https://www.researchobject.org/ro-crate/), from the metadata alone to a full preservation
package, and create a record from an RO-Crate - a detached crate, or an attached package or bag (see
[Import](#import)). They target the
[RO-Crate 1.3 specification](https://w3id.org/ro/crate/1.3).

| Export | You get | What the package contains |
|---|---|---|
| **RO-Crate** (`rocrate`) | a JSON-LD document | The record's metadata, its files referenced by URL, and the MD5 Invenio stores for each file |
| **RO-Crate (ZIP with files)** (`rocrate-zip`) | `<id>.zip` | The same as an attached crate, plus the model's other metadata exports and the generated README (`metadata/`) and every stored file the user may read (`data/`); a remote file is referenced by its URL |
| **RO-Crate (BagIt)** (`rocrate-bagit`) | `<id>-bagit.zip` | The ZIP's content as the payload of a [BagIt](https://www.rfc-editor.org/rfc/rfc8493) bag: MD5 and SHA-512 manifests, a tag manifest and the record's history as RO-Crate actions |
| **RO-Crate (AIP)** (`rocrate-aip`) | `<id>-aip.zip` | The bag plus everything the repository knows about the record: the full expanded record, hidden files, versions, requests, curation checks, audit log, JSON Schema and software environment |

Every archive is streamed - one chunk of one file in memory at a time, nothing staged on disk - and
carries the `md5` Invenio stores for each file, with the
[Workflow Run RO-Crate](https://www.researchobject.org/ro-terms/workflow-run) checksum terms. All but
the AIP are offered on the landing page; the AIP is listed only for holders of `aip-access` (see
[AIP export](#aip-export) below).

The full reference (the crate's content, every mapped property, customization) is in the API
documentation of `oarepo_formats.rocrate` and its modules.

## Requirements

The RO-Crate is converted from the model's DataCite JSON export
(`application/vnd.datacite.datacite+json`), so the model must have one; RDM models get it from
oarepo-rdm. Showing the AIP export on the landing page only to holders of `aip-access` needs
oarepo-ui 13.7.0 or later.

## Export

The four exports in the table above are separate customizations; a model adds the ones it offers.

### RO-Crate and ZIP export

Add the exports to an [oarepo-model](https://github.com/oarepo/oarepo-model) model:

```python
from oarepo_formats.rocrate import AddROCrateExport, AddROCrateZipExport

customizations = [
    AddROCrateExport(),  # JSON-LD metadata document
    AddROCrateZipExport(),  # ZIP with ro-crate-metadata.json and all files
]
```

Both are offered on the landing page and served by the REST API for their media types:

```bash
curl -H 'Accept: application/ld+json; profile="https://w3id.org/ro/crate"' \
  https://<repository>/api/<model>/<id>

curl -H 'Accept: application/zip; profile="https://w3id.org/ro/crate"' \
  https://<repository>/api/<model>/<id> -o crate.zip
```

The `profile` parameter must be quoted: werkzeug cuts an unquoted URI at the first `:`, so the
unquoted form shown in the spec does not select the export.

The BagIt and AIP exports are added the same way and are described in their own sections below.

The ZIP export checks the `read_files` permission and `RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE`
like Invenio's `/files-archive` before anything is streamed. Invenio sets no size limit by default -
set one for production.

The ZIP contains the RO-Crate metadata, the model's other metadata exports and the record's files:

```text
<id>.zip
├── ro-crate-metadata.json
├── README.generated.md        # the README export, if the model has it
├── metadata/
│   ├── datacite.xml           # the DataCite XML, Dublin Core and schema.org exports of RDM models
│   ├── dublin-core.xml
│   └── schemaorg.jsonld
└── data/
    └── <file key>             # the record's files the user may read
```

Which exports are added is set by `AddROCrateZipExport(include_exports={code: path, ...})`; an export
the model does not have, or one that fails (logged), is left out. With `OAREPO_ROCRATE_PROFILE` set in
`invenio.cfg`, the root declares `conformsTo` that profile.

### BagIt export

`AddROCrateBagItExport` adds a third export (code `rocrate-bagit`): the RO-Crate ZIP's content as the
payload of a BagIt bag (RFC 8493), for preservation. It is a valid zipped bag any BagIt validator can
check, and an OAIS *Archival Information Package* of a record version when the caller may read the
record's files and the version is published.

```python
from oarepo_formats.rocrate import AddROCrateBagItExport, AddROCrateExport, AddROCrateZipExport

customizations = [
    AddROCrateExport(),
    AddROCrateZipExport(),
    AddROCrateBagItExport(),
]
```

```bash
curl -H 'Accept: application/zip; profile="https://www.rfc-editor.org/rfc/rfc8493"' \
  https://<repository>/api/<model>/<id> -o <id>-bagit.zip
```

The export is offered on the landing page next to the other formats and served by the REST API for its
media type; `AddROCrateBagItExport(display=False)` keeps it off the landing page, for a deployment that
wants it on request only.

Configuration in `invenio.cfg`:

| Key | Default | Meaning |
|---|---|---|
| `OAREPO_ROCRATE_BAGIT_PROFILE` | `None` | an RO-Crate profile the crate in the bag declares, next to `OAREPO_ROCRATE_PROFILE` |
| `OAREPO_ROCRATE_BAGIT_INFO` | `{}` | constant `bag-info.txt` fields, e.g. `{"Source-Organization": "...", "Contact-Email": "..."}` |

!!! note "File keys with a literal `%`"
    RFC 8493 requires `%` in a manifest path to be percent-encoded (`%25`), and the export does that.
    bagit-python 1.9 decodes only `%0D` and `%0A`, so it reports such a bag as incomplete.

The layout, the streaming order, the fixity rules and the provenance are described in
[oarepo_formats.rocrate.exports.bag][], [oarepo_formats.rocrate.exports.provenance][] and
[oarepo_formats.rocrate.model][]. Users with `Administration()` are not subject to
`RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE`, neither for the bag nor for the ZIP.

### AIP export

`AddROCrateAIPExport` adds a fourth export (code `rocrate-aip`): the record's **Archival Information
Package** - the BagIt export extended with everything the repository knows about the record (the complete
expanded record, hidden files, the file metadata, the versions, the requests with their timelines, the
curation checks, the audit log, the JSON Schema and the software environment) and without a download
size limit.

```python
from oarepo_formats.rocrate import AddROCrateAIPExport

customizations = [
    AddROCrateExport(),
    AddROCrateZipExport(),
    AddROCrateBagItExport(),
    AddROCrateAIPExport(),
]
```

It is available only to the holders of the `aip-access` action, granted as any Invenio action:

```bash
invenio access allow aip-access user <email>
invenio access allow aip-access role <role>

curl -H 'Accept: application/vnd.oarepo.aip+zip' \
  https://<repository>/api/<model>/<id> -o <id>-aip.zip
```

A caller without `aip-access` gets a **403**. The export does not grant read access to a record: a
preservation officer who should be able to open restricted records needs the action in the model's
permission policy as well - which records may be preserved is the deployment's decision, not
`oarepo-formats`':

```python
from invenio_rdm_records.services.permissions import RDMRecordPermissionPolicy

from oarepo_formats.rocrate.permissions import AIPAccess


class MyRecordPermissionPolicy(RDMRecordPermissionPolicy):
    can_read = [*RDMRecordPermissionPolicy.can_read, AIPAccess()]
    can_read_draft = [*RDMRecordPermissionPolicy.can_read_draft, AIPAccess()]
    can_read_deleted = [*RDMRecordPermissionPolicy.can_read_deleted, AIPAccess()]
    can_read_files = [*RDMRecordPermissionPolicy.can_read_files, AIPAccess()]
    can_draft_read_files = [*RDMRecordPermissionPolicy.can_draft_read_files, AIPAccess()]
```

The export is listed on the landing page only for holders of `aip-access` (its `display_if`, which the
sidebar of oarepo-ui 13.7.0 or later evaluates per request; an older oarepo-ui lists it for everyone, who
then get 403), reads the record with `system_identity`
after the entry check, and records itself in the audit log (`record.aip_export`) when audit logging is
enabled. `OAREPO_ROCRATE_AIP_COLLECTORS` adds a deployment's own collectors. The package, its members and
the collectors are described in [oarepo_formats.rocrate.exports.aip][]; the access action and the generator in
[oarepo_formats.rocrate.permissions][].

### Changing the mapping

The export's conversion is a marshmallow schema (DataCite JSON to nested schema.org JSON-LD) that is then
flattened into the RO-Crate document - the same pattern as Invenio's own serializers.

#### What the schema gets

!!! note "The schema gets both the DataCite metadata and the record"
    Every field and method of the schema receives one object with two keys:

    ```python
    obj = {
        "datacite": {...},  # the DataCite attributes of the model's DataCite JSON export
        "record": {...},  # the serialized record: the API JSON of the record
    }
    ```

    `record` is the same dict a REST client gets for the record (`id`, `metadata`, `links`,
    `files.entries`, ...), already filtered by the permissions of the requesting user. It is **not** the
    database record object, so only what the record's API JSON exposes is available.

The default mapping takes everything from `datacite`, except what DataCite does not have: the files
(`hasPart`, from `record["files"]["entries"]`) and the landing page (the root `@id` of a record without
a PID, from `record["links"]["self_html"]`).

!!! warning "`record["expanded"]` is present only when the record was read with `expand=True`"
    The record is read by the view that serves the export, and the views differ:

    | Export requested via | `expand` | `record["expanded"]` |
    |---|---|---|
    | the landing page download (`/records/<pid>/export/<code>`) | `True` | present |
    | the REST API with the export's `Accept` header (`/api/<model>/<id>`, `/draft`, `/latest`) | `False`, unless the client adds `?expand=true` | missing |

    The same applies to the exports in the ZIP and to the README export, which get the same record.
    The default mapping does not use `expanded`, so its output is the same on both paths. A schema that
    uses the expanded data (e.g. resolved vocabulary titles) must not fail without it - fall back to
    the unexpanded value:

    ```python
    def get_credit_text(self, obj):
        expanded = obj["record"].get("expanded", {})  # only with expand=True
        owner = expanded.get("parent", {}).get("access", {}).get("owned_by", {})
        return owner.get("username") or obj["record"]["id"]
    ```

    and tell REST clients to request the export with `?expand=true` if they need the expanded output.

#### Subclassing the schema

To change or add a mapping, subclass the schema and pass it to both exports. A method can use either
key - here a property from the record's own metadata, which DataCite does not carry:

```python
from marshmallow import fields

from oarepo_formats.rocrate import AddROCrateExport, AddROCrateZipExport
from oarepo_formats.rocrate.exports.schema import DataCiteToROCrateSchema


class MySchema(DataCiteToROCrateSchema):
    creditText = fields.Method("get_credit_text")

    def get_keywords(self, obj):  # from DataCite
        return [s["subject"] for s in obj["datacite"].get("subjects", [])]

    def get_credit_text(self, obj):  # from the record
        return f"{obj['record']['metadata']['title']} ({obj['record']['id']})"


customizations = [
    AddROCrateExport(schema_cls=MySchema),
    AddROCrateZipExport(schema_cls=MySchema),
]
```

Changes outside the root data entity (e.g. a license of the metadata itself) need a subclass of
`oarepo_formats.rocrate.exports.serializer.ROCrateSerializer`; additional ZIP members a subclass of
`oarepo_formats.rocrate.exports.zip.ROCrateZipSerializer`.

## Import

`AddROCrateImport` adds a route that creates a record from an RO-Crate and brings its files along. Three
kinds of input are accepted, told apart by the media type:

| Body | Input | The files come from |
|---|---|---|
| `application/ld+json` | a **detached** crate: one JSON-LD document | their URLs, downloaded by one task after the response |
| `application/zip` | an **attached** package: the `rocrate-zip` export, or a bag | the package itself, written in the request |
| `application/json` `{"url": "..."}` | an attached package **behind a URL** | the package, read by ranges in a task |

```python
from oarepo_formats.rocrate import AddROCrateImport

customizations = [
    AddROCrateImport(),
    AddROCrateExport(),  # optional, but this is what produces an importable crate
]
```

```bash
# a detached crate
curl -X POST 'https://<repository>/api/<model>/import/rocrate?publish' \
  -H 'Content-Type: application/ld+json' --data-binary @crate.json

# a package in the body: the rocrate-zip or rocrate-bagit export of a record
curl -X POST 'https://<repository>/api/<model>/import/rocrate' \
  -H 'Content-Type: application/zip' --data-binary @crate.zip

# a package behind a URL - a *static* object (S3, a web server, a stored file), never this package's own
# streamed export links
curl -X POST 'https://<repository>/api/<model>/import/rocrate?publish' \
  -H 'Content-Type: application/json' -d '{"url": "https://data.example.org/crate.zip"}'
```

| Query | Meaning |
|---|---|
| `?publish` | publish the imported record once its files are in place |
| `?notifications=off` | do not mail the two "ok" outcomes; a failure is always mailed |
| `?metadata=record-json` | an attached package: take the record's own JSON instead of the crate (see below) |
| `?files=reference` | keep referenced files as remote files where allowed, instead of downloading them (see below) |

`?publish` and `?notifications` take only a boolean (`?publish`, `?publish=false`, `?notifications=on|off`),
`?metadata` only `crate` or `record-json` and `?files` only `fetch` or `reference`; anything else is a
**400**, so a typo can not quietly leave an unpublished draft.

### Crates from other tools

Crates do not have to come from this package: an electronic lab notebook or any other RO-Crate tool may
write them. The import reads the metadata document as plain JSON, as the RO-Crate specification intends, and
accepts the usual variations of the same meaning:

- property names and `@type`s as **prefixed or full IRIs** - `schema:name`, `http://schema.org/name`,
  `https://schema.org/name`, `dct:conformsTo`, ...;
- **value objects** where a plain value is expected - `{"@value": "Experiment 42", "@language": "en"}`.

It does not run a JSON-LD processor and never fetches a context URL. A crate whose own (inline) `@context`
gives a term the import reads another meaning - `"name": "https://example.org/terms#sampleName"`, or
`"hasPart": null` - is refused with a **400** naming the term, rather than misread. A context that only adds
terms (a tool's own vocabulary) is fine; properties the import does not know are ignored.

### Referenced files

A file the crate does not carry is *referenced*: every file of a detached crate, and a web-based `File` of
an attached package - one whose `@id` is an absolute `http(s)` URL. This package's archives describe a
record's remote files (Invenio's `R` transfer, content stored elsewhere) that way, so a record with stored
and remote files exports to a package with both, and imports from it again.

| `?files=` (`OAREPO_ROCRATE_IMPORT_REFERENCED_FILES`) | A referenced file becomes |
|---|---|
| `fetch` (default) | a stored file: downloaded by one task after the response; its host must be in `RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS`, and its `contentSize`, `md5` and `sha512` are verified |
| `reference` | a remote file pointing at the URL, if its domain is in `RECORDS_RESOURCES_FILES_ALLOWED_REMOTE_DOMAINS`; otherwise downloaded as with `fetch` |

A referenced file's URL is its `contentUrl` if it has one, else its `@id`. In this package's ZIP and BagIt
exports a remote file has only the source record's `/content` link as `@id` (Invenio does not expose a remote
file's own URL), so a reference points at the source repository, which redirects to the content; the AIP adds
the remote URL as `contentUrl`. A package's stored files are still written from the archive; its referenced
files are downloaded by the same task, which finishes the import afterwards.

The answer is always `201` with
`{"record": ..., "import": {"files": [...], "publish": null | "published" | "scheduled"}}`. A crate the
model refuses (its file plan, the metadata) is refused with **400** before anything is created.

### Attached packages

An attached package is a ZIP carrying its own metadata (`ro-crate-metadata.json`) next to the data: the
**`rocrate-zip`** export, or a **BagIt bag** (`rocrate-bagit`) - a `<id>/` directory with `bagit.txt`,
`bag-info.txt` and the manifests. Which one it is does not matter for the client: the import recognises
both, and a package re-zipped around a folder (with `__MACOSX/`, `.DS_Store`) as well.

- **The payload is the file source.** A file the package carries is written from it - its `contentUrl`
  (which points at the exporting repository) is not fetched - so the package's own files land in the record
  byte for byte, and no file host has to be allow-listed for them.
- **Metadata members are not files.** The export describes its own included exports (`README.generated.md`,
  `metadata/record.json`, `metadata/datacite.xml`, ...) in the crate; those are not imported as record
  files.
- **Fixity.** A sha512 mismatch against the bag's `manifest-sha512.txt` always fails the member: it means
  the bytes changed *after* the export. An md5 mismatch (the checksum Invenio stored at upload) follows
  `OAREPO_ROCRATE_IMPORT_VERIFY`: `strict` fails the member, `log` imports it and logs.
- **A body is imported in the request**, in one unit of work: a bad member refuses the whole import with a
  **400** and nothing is created. It is meant for packages up to
  `OAREPO_ROCRATE_IMPORT_MAX_ARCHIVE_SIZE` (100 MB by default) and **sends no mail** - the response is the
  report.
- **A URL is imported by a task.** The request reads only the archive's table of contents and the metadata,
  so a large package is never downloaded as a whole; the files are then read member by member and written
  by one task, which mails the outcome as the detached import does. The URL must serve a **static object**
  with `Accept-Ranges: bytes` and a stable `ETag`: this package's own `/export/...` links are streamed and
  change on every request, so they are refused with a **400** (post the file as the body instead). The
  URL's host must be in `RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS`, and the URL has to stay valid for
  roughly the size of the archive divided by the transfer rate. A dropped connection is retried (three
  times, with a growing delay); when the retries run out, the files that were not written are removed from
  the draft and the failure is mailed - a file that failed is never left `pending`.
- **A package may also reference files.** A *web-based* file of the crate (an absolute `@id` instead of a
  member path) is not in the package: it is fetched, or kept as a remote file - see
  [Referenced files](#referenced-files).
- **An AIP is imported like any other bag** - which is a partial import, see the warning below.
- **A failed body import leaves the bytes it already wrote in the bucket.** Storage writes are not
  transactional: the database rows of a rolled-back import are gone, but the objects that were streamed
  before the failure stay as orphans, exactly as with a failed normal upload. It is bounded by
  `OAREPO_ROCRATE_IMPORT_MAX_ARCHIVE_SIZE` and accepted; there is no cleanup code.

!!! warning "Importing an AIP creates a new record from it - it does not restore the record"
    An AIP (`rocrate-aip`) is imported like a BagIt package: the metadata (from the crate, or from its
    `record.json` with `?metadata=record-json`) and the payload files - with what its
    `metadata/repository/files.json` says about each file: whether it is **hidden** (`access.hidden`) and
    the **file metadata** set on it (a photo's title, ...). Everything else it carries is **not** restored:

    - **The repository members are dropped**: versions, requests, curation checks, the audit log, the JSON
      Schema and the software environment (`metadata/repository/`). The imported record is a new record
      with a single version, its own PID, owner and history.
    - **Remote files** are read from their `contentUrl` (the remote URL the AIP keeps): fetched by default,
      or kept as remote files with `?files=reference` (see [Referenced files](#referenced-files)).

    A `files.json` that is present but can not be read refuses the import (**400**): ignoring it would turn
    the hidden files into visible ones. Any package that carries the member gets the same treatment, not only
    an AIP; the rest of a file's entry (size, checksum, storage) describes the source and is not taken.
### The metadata an attached package brings

By default the metadata is the crate's, converted to DataCite and handed to the model's DataCite import,
exactly as for a detached crate - portable, but lossy: DataCite can not express everything a model has.

```python
# invenio.cfg
OAREPO_ROCRATE_IMPORT_METADATA = "record-json"
```

With `record-json`, a package that carries `metadata/record.json` (a bag does by default) is imported from
the record's **own API JSON** instead: lossless for the model that produced it, meaningless for another -
a restore, not an exchange. It is handed to the model's `json` import with only its `metadata` and
`custom_fields`: everything else describes the source repository (identifiers - a `pids` entry would claim
its DOI -, ownership, access, state, versions, links), and a package whose `$schema` names another model is
refused.

### What the deployment has to provide

1. **A DataCite import** - or a mapping of its own (see [Your own mapping](#your-own-mapping)). By
   default the crate is converted to DataCite JSON (`application/vnd.datacite.datacite+json`) and handed to
   the model's own DataCite import, which the deployment writes with `AddMetadataImport` - there is no
   generic DataCite importer. `AddROCrateImport` finds it by that mimetype; `base_import=` names another
   import, and `deserializer=` bypasses the lookup.
2. **The file hosts in the allow-list.** A referenced file is downloaded, so its host must be in
   `RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS` - whole netlocs, port included (`["data.example.org:8443"]`).
   With `?files=reference` a file whose host is in `RECORDS_RESOURCES_FILES_ALLOWED_REMOTE_DOMAINS` stays
   remote instead. A URL outside the lists refuses the import with **400** and creates nothing. Redirects are
   followed by this package and each hop must be allowed as well.
3. **No extra permissions - but a user.** The download runs as the importing user, in their own draft:
   `create` (and `draft create files`) is enough, `publish` with `?publish`; nothing has to be granted to
   `SystemProcess()`. Because the task acts as that user, an import whose files are written after the request is
   refused with **400** when it is not made by a user (the system process, an anonymous identity).

### Your own mapping

The crate's root data entity is converted by `schema_cls`, and the result is handed to the model's import
that `base_import` names: **`base_import` declares which import is used after the conversion.** The
default pair is `ROCrateToDataCiteSchema` and the model's DataCite import (`DATACITE_JSON_MIMETYPE`).

One import is special: **`IDENTITY_IMPORT`** (`"application/json"`), the model's built-in `json` import,
which hands the record JSON over unchanged. A schema that produces the record's own structure instead of
DataCite is paired with it - for a model without a DataCite import, or for a mapping that is lossless for
the model's properties:

```python
from marshmallow import Schema, fields

from oarepo_formats.rocrate import IDENTITY_IMPORT, AddROCrateImport
from oarepo_formats.rocrate.imports.jsonld import as_list, text


class CrateToRecordSchema(Schema):
    metadata = fields.Method("get_metadata")

    def get_metadata(self, root):
        return {
            "title": text(root.get("name")),
            "keywords": [text(k) for k in as_list(root.get("keywords")) if text(k)],
        }


customizations = [
    AddROCrateImport(schema_cls=CrateToRecordSchema, base_import=IDENTITY_IMPORT),
]
```

The schema gets the reader's view of the crate: the root data entity with the references below it expanded
(three levels deep), its property names and `@type`s written as the RO-Crate terms and value objects left
as they are. `oarepo_formats.rocrate.imports.jsonld` has the helpers to read it - `text` (also of a value
object), `literal`, `as_list`, `types` and `reference_id`. The model's service validates the result as it
validates any create: a record that does not fit is refused with **400** before anything is created.

!!! note "What the identity import takes"
    The crate is untrusted input, so whatever reaches `IDENTITY_IMPORT` is cut down to `metadata` and
    `custom_fields` (`PORTABLE_FIELDS`): a schema that copies crate properties can not set the record's
    identifiers (`pids`), ownership, access or state. The package's own `record.json`
    (`?metadata=record-json`) goes the same way.

Going through DataCite keeps one mapping for every model with a DataCite import; an own schema is per model,
but loses nothing DataCite can not express.

### What happens, and when

The user gets the `201` as soon as the draft exists. Then **one** celery task writes whatever is left: the
member files of a package behind a URL (read from it by HTTP ranges), and the referenced files of any import
(downloaded). It writes them one at a time (a unit of work per file, so completed files survive a later
failure), verifies them, and finishes the import itself - no polling. A dropped connection is retried with a
growing delay; a failure deletes the files nobody will write any more and mails it. The record is created and
its files are planned in one unit of work, so a refused file plan or a missing permission leaves nothing
behind.

- **`?publish` without files** publishes in the request; a refusal refuses the whole import.
- **`?publish` with files** only checks the permission in the request and leaves the publish to the task: a
  draft with unfinished files can not be published. The answer says `"scheduled"`.
- **The task** publishes only if the draft is unchanged since the import, and it never publishes twice: a
  record the user published themselves, a draft the user deleted and a draft the user edited are left alone
  (an edit is reported as a `modified` failure).
- **A model without drafts** creates its (published) record and imports the files as they are; `?publish`
  has no effect.

A download has a timeout (`OAREPO_ROCRATE_IMPORT_ARCHIVE_TIMEOUT`) and at most three redirects, each checked
against the allow-list (a redirect without `Location` is refused). The body is asked for unencoded, and
decoded when a server compresses it anyway. It is refused when it is longer than the crate's `contentSize`
(or, without one, than the server's `Content-Length`), and above `OAREPO_ROCRATE_IMPORT_MAX_FILE_SIZE` -
which is also where a body nobody states the size of is cut off. Its md5 is compared with the crate's md5
(`OAREPO_ROCRATE_IMPORT_VERIFY`) and a stated sha512 always fails the file on a mismatch; another checksum
(sha1, sha256) is not verified. A dropped connection and a status a later attempt may not get (408, 425,
429, 500, 502, 503, 504) are retried, with a growing delay; any other HTTP error fails the file. A failed
file is deleted from the draft, so nothing `pending` is left behind, and a retried task still reports the
files an earlier attempt failed. When an archive behind a URL breaks, its remaining members fail with it,
but the package's downloads are still written.

### Mails

The importer is told the outcome by e-mail (invenio-notifications):

| `type` | Sent when |
|---|---|
| `rocrate-import.failed` | a file failed, did not match its checksum or was longer than the crate says, the package behind a URL could not be read, the draft was edited, or the deferred publish was refused |
| `rocrate-import.completed` | every file arrived and no publish was requested |
| `rocrate-import.published` | the deferred publish committed |

The failure mail names the files that failed, each with its error - the one-line reason this package found
(`HTTP 404`, a checksum mismatch), never the remote host's response body. The html part is escaped: the title
comes from the crate and the error from an untrusted host.

The templates are in `oarepo_formats/rocrate/templates/semantic-ui/invenio_notifications/` and can be
overridden by placing a file of the same name in the deployment's templates. The builders are registered
in `NOTIFICATIONS_BUILDERS`; a deployment's own builder for the same `type` wins.

!!! note "No mail without a user"
    The mails, and the deferred publish itself, run as the importing user. An import made with a token that
    has no user identity therefore mails nobody; `?publish` with files is refused with a **400** for such a
    caller, rather than scheduled and never done. `?publish` without files still works (it runs in the
    request), as does importing without `?publish` at all.

### Configuration

| Key | Default | Meaning |
|---|---|---|
| `OAREPO_ROCRATE_IMPORT_FILES` | `True` | whether the crate's files are imported; `False` imports the metadata only |
| `OAREPO_ROCRATE_IMPORT_MAX_FILES` | `100` | refuse a crate with more file entities |
| `OAREPO_ROCRATE_IMPORT_ARCHIVE` | `True` | accept attached packages (body and URL) at all |
| `OAREPO_ROCRATE_IMPORT_MAX_ARCHIVE_SIZE` | `100 MB` | largest package accepted in the body |
| `OAREPO_ROCRATE_IMPORT_ARCHIVE_TIMEOUT` | `30` | connect/read timeout of a URL's ranged reads and of a downloaded file |
| `OAREPO_ROCRATE_IMPORT_MAX_FILE_SIZE` | `50 GB` | largest file a download writes; a body of unknown size is cut off here |
| `OAREPO_ROCRATE_IMPORT_VERIFY` | `strict` | `strict`/`log` for an md5 mismatch (sha512 always fails) |
| `OAREPO_ROCRATE_IMPORT_METADATA` | `crate` | `crate` or `record-json`: where an attached package's metadata comes from |
| `OAREPO_ROCRATE_IMPORT_REFERENCED_FILES` | `fetch` | `fetch` or `reference`: what happens to a referenced file (see above) |

A package's member list is checked with invenio's own ZIP limits, `RECORDS_RESOURCES_ZIP_MAX_ENTRIES` and
`RECORDS_RESOURCES_ZIP_MAX_RATIO` (for deflated members), so an archive the files API would refuse to
preview is refused here as well.

The route, the crate conversion, the archive reader, the tasks and the mails are described in
[oarepo_formats.rocrate.imports][], [oarepo_formats.rocrate.imports.crate][],
[oarepo_formats.rocrate.imports.crate_datacite][], [oarepo_formats.rocrate.imports.archive][],
[oarepo_formats.rocrate.imports.tasks][] and [oarepo_formats.rocrate.imports.notifications][].

## Tests

The mapping is pinned by `tests/rocrate/data/rocrate_full.json`, the crate of DataCite's full example
record. After an intended change of the mapping, review the diff and regenerate it with
`.venv/bin/python -m tests.rocrate.exports.test_schema`.

The import's conversion back is pinned the same way, by `tests/rocrate/data/datacite_round_trip.json`
(the full example exported to a crate and converted back to DataCite): regenerate it with
`.venv/bin/python -m tests.rocrate.imports.test_crate_datacite`. The difference between that file and
`datacite_full.json` is exactly what a round trip loses.

The integration tests (`tests/api_tests`) build a real oarepo-model model and need the docker
services, so run them with `./run.sh test` (not `--skip-services`).
