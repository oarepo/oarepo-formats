# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

r"""RO-Crate formats of oarepo records: exports and the import of a detached crate.

Adds four exports to an oarepo-model model that describe a record as an
[RO-Crate](https://w3id.org/ro/crate/1.3): a JSON-LD document with the record's metadata and links to
its files - a *Detached RO-Crate Package* (spec 4.4) - and a ZIP archive with the metadata, the
record's other metadata formats and its files, as the crate alone, inside a BagIt bag, or as the full
Archival Information Package (see below); and one import that creates a record from such a detached
crate. Section numbers ("spec 4.4") refer to the
[RO-Crate 1.3 specification](https://w3id.org/ro/crate/1.3).

To use them, import the customizations `AddROCrateExport`, `AddROCrateZipExport`,
`AddROCrateBagItExport`, `AddROCrateAIPExport` and `AddROCrateImport` from `oarepo_formats.rocrate`.
To customize them, import from the modules: [oarepo_formats.rocrate.exports.schema][] (the mapping and its
helpers), [oarepo_formats.rocrate.exports.serializer][], [oarepo_formats.rocrate.exports.zip][],
[oarepo_formats.rocrate.exports.bag][], [oarepo_formats.rocrate.exports.aip][], [oarepo_formats.rocrate.imports][],
[oarepo_formats.rocrate.imports.tasks][] and [oarepo_formats.rocrate.imports.notifications][].

## Requirements

The RO-Crate is converted from DataCite, so the model must have a DataCite JSON export registered
under the mimetype `application/vnd.datacite.datacite+json`. RDM models get one from oarepo-rdm;
other models register their own:

```python
from oarepo_model.customizations import AddMetadataExport

AddMetadataExport(
    code="datacite-json",
    name=_("DataCite JSON"),
    mimetype="application/vnd.datacite.datacite+json",
    serializer=MyDataCiteJSONSerializer(),
)
```

Without it, the RO-Crate export fails with `ValueError("No export found ...")`.

## Usage

Add [AddROCrateExport][oarepo_formats.rocrate.model.AddROCrateExport] to the model's customizations
(in `model.py`):

```python
from oarepo_formats.rocrate import AddROCrateExport

customizations = [
    AddROCrateExport(),
]
```

The export (code `rocrate`, file extension `.json`) is then offered on the landing page and served
by the REST API for the RO-Crate media type:

```bash
curl -H 'Accept: application/ld+json; profile="https://w3id.org/ro/crate"' \
    https://<repository>/api/<model>/<id>
```

The `profile` parameter must be quoted. The unquoted form shown in the spec
(`application/ld+json;profile=https://w3id.org/ro/crate`) is cut at the first `:` by werkzeug and
does not select the export. A search request with this header returns a JSON array with one crate
per hit.

## ZIP archive with the files

[AddROCrateZipExport][oarepo_formats.rocrate.model.AddROCrateZipExport] adds a second export (code
`rocrate-zip`): a streamed ZIP archive - an *Attached RO-Crate Package* (spec 4.3) - with the RO-Crate
metadata, the model's other metadata exports and all files the user may read. It takes the same
`schema_cls` as `AddROCrateExport`:

```python
from oarepo_formats.rocrate import AddROCrateExport, AddROCrateZipExport

customizations = [
    AddROCrateExport(),
    AddROCrateZipExport(),
]
```

```bash
curl -H 'Accept: application/zip; profile="https://w3id.org/ro/crate"' \
    https://<repository>/api/<model>/<id> -o crate.zip
```

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

The permission check and `RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE` (no limit by default - set it)
are the same as for Invenio's `/files-archive` and fail before anything is streamed. It needs an
oarepo-runtime with streaming exports (`Export.streaming`). In the archive, files are referenced by
their path, with the download URL as `contentUrl`, and the root data entity is `./`.

### Metadata exports in the archive

The archive carries the output of other exports of the model, unmodified, so that a downloaded dataset
keeps its metadata in the formats the repository offers - and, with the
[README export][oarepo_formats.readme], describes itself to a human reader as well. `include_exports`
maps export codes to archive paths; the default (`DEFAULT_INCLUDED_EXPORTS` in
[oarepo_formats.rocrate.exports.zip][]) is the README and the DataCite XML, Dublin Core and schema.org JSON-LD
exports of RDM models:

```python
from oarepo_formats.rocrate.exports.zip import DEFAULT_INCLUDED_EXPORTS

AddROCrateZipExport(
    include_exports={
        **DEFAULT_INCLUDED_EXPORTS,
        "marcxml": "metadata/marc21.xml",
    }
)
```

- An export the model does not have is left out, as is an export that fails (the error is logged to
  the application logger).
- Each export is a `File` in the root's `hasPart` and `subjectOf`, `about` the root, with its mimetype
  as `encodingFormat` and its `oai_schema` (the schema it declares for OAI-PMH) as `conformsTo`, if it
  has one.
- `include_exports={}` adds none.

### Profile

If `OAREPO_ROCRATE_PROFILE` is set in `invenio.cfg` (see [oarepo_formats.rocrate.config][]), the root
of every ZIP declares `conformsTo` that profile and the profile is added as a contextual entity. Set
it only once the profile is published at its `@id` - e.g. a multi-metadata profile with the rules
above. It is not set by default.

## The BagIt export

[AddROCrateBagItExport][oarepo_formats.rocrate.model.AddROCrateBagItExport] adds a third export (code
`rocrate-bagit`): the RO-Crate ZIP's content as the payload of a
[BagIt](https://www.rfc-editor.org/rfc/rfc8493) bag, streamed the same way - one chunk of one file in
memory at a time, exact `Content-Length`. It is a preservation package (see
[oarepo_formats.rocrate.exports.bag][]), offered on the landing page like the other exports (`display=False`
keeps it out, for a deployment that wants it on request only):

```python
from oarepo_formats.rocrate import AddROCrateExport, AddROCrateZipExport, AddROCrateBagItExport

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

```text
<id>-bagit.zip
└── <id>/                         # the bag
    ├── bagit.txt
    ├── bag-info.txt
    ├── data/                     # the RO-Crate, as in the ZIP export
    ├── manifest-md5.txt          # the stored MD5s, so a corrupted file makes the bag invalid
    ├── manifest-sha512.txt       # computed while streaming
    └── tagmanifest-sha512.txt
```

The package is an OAIS *Archival Information Package* of a record version when the caller may read the
record's files and the version is published; `bag-info.txt` records the record state and the version
group, so a package taken from a draft can not be mistaken for one. Besides the metadata exports of the
ZIP it carries the record's own API JSON (`metadata/record.json`), and its crate describes the
provenance of the record with RO-Crate actions
([oarepo_formats.rocrate.exports.provenance][]).

Configuration: `OAREPO_ROCRATE_BAGIT_PROFILE` (a profile declared on the crate, like
`OAREPO_ROCRATE_PROFILE`) and `OAREPO_ROCRATE_BAGIT_INFO` (constant `bag-info.txt` fields), see
[oarepo_formats.rocrate.config][]. Administrators (`Administration()`) are not subject to
`RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE`, for this export and for the ZIP.

## The AIP export

[AddROCrateAIPExport][oarepo_formats.rocrate.model.AddROCrateAIPExport] adds a fourth export (code
`rocrate-aip`): the **Archival Information Package** of a record - the BagIt export extended with
everything the repository knows about the record (the complete expanded record, hidden files, the file
metadata, the versions, the requests with their timelines, the curation checks, the audit log, the JSON
Schema and the software environment) and without a download size limit. It is available only to holders
of the `aip-access` action (see [oarepo_formats.rocrate.permissions][]) - both granted and enforced by
this package, and listed on the landing page only for them - and answers 403 to everyone else:

```python
from oarepo_formats.rocrate import AddROCrateAIPExport

customizations = [
    AddROCrateExport(),
    AddROCrateZipExport(),
    AddROCrateBagItExport(),
    AddROCrateAIPExport(),
]
```

```bash
invenio access allow aip-access user <email>
curl -H 'Accept: application/vnd.oarepo.aip+zip' \
    https://<repository>/api/<model>/<id> -o <id>-aip.zip
```

The export does not grant read access to a record: add `AIPAccess()` to the model's read permissions so
that a preservation officer can open a restricted one (see [oarepo_formats.rocrate.permissions][]). The
package and its collectors are described in [oarepo_formats.rocrate.exports.aip][];
`OAREPO_ROCRATE_AIP_COLLECTORS` adds a deployment's own collectors.

## The import of a detached crate

[AddROCrateImport][oarepo_formats.rocrate.model.AddROCrateImport] adds
`POST <model url_prefix>/import/rocrate[?publish][&notifications=off]` to the model's API: the posted
*Detached RO-Crate Package* becomes a record - a draft, unless the model has no drafts - and its files
are downloaded into it with the **Fetch** transfer of invenio-records-resources. `?publish` publishes
the record once the downloads finish, and the uploader is mailed the outcome (the failure always, the
other two unless `notifications=off`).

```python
from oarepo_formats.rocrate import AddROCrateImport

customizations = [
    AddMetadataImport(code="datacite", mimetype="application/vnd.datacite.datacite+json", ...),
    AddROCrateImport(),
    AddROCrateExport(),
]
```

```bash
curl -X POST 'https://<repository>/api/<model>/import/rocrate?publish' \
    -H 'Content-Type: application/ld+json' --data-binary @crate.json
```

The deployment provides two things (see [oarepo_formats.rocrate.imports][]):

- a **DataCite import** (mimetype above) the converted metadata is handed to - there is no generic
  DataCite importer; `base_import=` and `deserializer=` name another one;
- the crate's hosts in `RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS`, whole netlocs with the port. The files
  are downloaded by this package's own task, as the importing user - no `SystemProcess()` grant is needed.

The crate is converted by [oarepo_formats.rocrate.imports.crate_datacite][], the route is in
[oarepo_formats.rocrate.imports][], the download and the publish in [oarepo_formats.rocrate.imports.tasks][] and
the mails in [oarepo_formats.rocrate.imports.notifications][].

## The crate

- The **root data entity** has the URL of the record's PID as its `@id` - the DOI
  (`https://doi.org/<doi>`) by default, or the first identifier type of `OAREPO_FORMATS_PID_SCHEMES` the
  record has - otherwise the landing page URL; it is a `Dataset`. The PID is also its `cite-as`; it and
  the record's other identifiers are its `identifier`s.
- **Metadata** (titles, creators, contributors, publisher, dates, licenses, subjects, funding, geo
  locations, related works, ...) comes from the DataCite export; each property and its source is
  listed on [DataCiteToROCrateSchema][oarepo_formats.rocrate.exports.schema.DataCiteToROCrateSchema].
- **Files** are `File` data entities in the root's `hasPart`; their `@id` is the direct download URL
  (`<record>/files/<key>/content`). Files are listed only if the requesting user may see them (the
  `files.entries` of the serialized record); hidden and not yet committed files are skipped.
- **Checksums:** RO-Crate itself has no checksum property (it leaves fixity to BagIt), so every `File`
  carries the checksum Invenio stores for it - `md5` by default - with the checksum terms of the
  [Workflow Run RO-Crate vocabulary](https://www.researchobject.org/ro-terms/workflow-run), defined in
  the crate's `@context` (which is then `[<RO-Crate context>, {"md5": ...}]`). The members the ZIP adds
  (README, metadata exports) carry an `md5` as well. A consumer can check a downloaded file against
  it; generic RO-Crate tools show the property but do not verify it.
- **People, organizations, licenses, ...** are separate entities, identified by their ORCID, ROR,
  license URL, ... if they have one, otherwise by a local `#<type>-<hash>` identifier derived from
  their content, so the same person listed twice is one entity.

## Changing the mapping

Subclass [DataCiteToROCrateSchema][oarepo_formats.rocrate.exports.schema.DataCiteToROCrateSchema] - a marshmallow
schema whose fields are the schema.org properties of the root data entity - and pass it to the
export.

Every field and method of the schema gets `obj = {"datacite": ..., "record": ...}`: the DataCite
attributes of the model's DataCite JSON export **and the serialized record** - the record's API JSON
(`id`, `metadata`, `links`, `files.entries`, ...), filtered by the permissions of the requesting user,
not the database record object. The default mapping uses the record only for what DataCite does not
have (the files and the landing page); a subclass can map anything the API JSON exposes:

```python
from marshmallow import fields

from oarepo_formats.rocrate import AddROCrateExport
from oarepo_formats.rocrate.exports.schema import DataCiteToROCrateSchema


class MySchema(DataCiteToROCrateSchema):
    # obj is {"datacite": <DataCite attributes>, "record": <serialized record>}

    author = fields.Constant(None, dump_only=True)  # drop a property
    creditText = fields.Method("get_credit_text")  # add a property

    def get_keywords(self, obj):  # change a property
        return [s["subject"] for s in obj["datacite"].get("subjects", [])]

    def get_credit_text(self, obj):
        return f"{obj['datacite']['titles'][0]['title']}, {obj['record']['id']}"


AddROCrateExport(schema_cls=MySchema)
```

Fields may return nested objects (`{"@type": "Person", "name": ...}`); they become separate entities
of the crate automatically. The conventions are described in
[oarepo_formats.rocrate.exports.schema][]. Changes outside the root data entity (e.g. the license of the
metadata itself on the `ro-crate-metadata.json` descriptor) need a serializer subclass, see
[ROCrateSerializer][oarepo_formats.rocrate.exports.serializer.ROCrateSerializer].
"""

from __future__ import annotations

# the API for using the exports; extension points are imported from the modules. Explicit re-exports
# (PEP 484 `X as X`) not listed in __all__, so the API documentation shows them only on their module page
from .imports.resource import IDENTITY_IMPORT as IDENTITY_IMPORT
from .model import AddROCrateAIPExport as AddROCrateAIPExport
from .model import AddROCrateBagItExport as AddROCrateBagItExport
from .model import AddROCrateExport as AddROCrateExport
from .model import AddROCrateImport as AddROCrateImport
from .model import AddROCrateZipExport as AddROCrateZipExport
