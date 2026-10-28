# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Default configuration of the RO-Crate format; values in `invenio.cfg` win."""

from __future__ import annotations

from typing import Any

OAREPO_ROCRATE_PROFILE: dict[str, Any] | None = None
"""The RO-Crate profile the ZIP archives conform to, as its contextual entity; `None` declares no profile.

When set, the root data entity of every RO-Crate ZIP declares `conformsTo` this profile and the entity
is added to the crate (with `@type` `["CreativeWork", "Profile"]` unless given). The `@id` must resolve
to the published, human-readable profile description:

```python
OAREPO_ROCRATE_PROFILE = {
    "@id": "https://example.org/profiles/inveniordm-multi-metadata/1.0",
    "name": "InvenioRDM Multi-Metadata RO-Crate Profile",
    "version": "1.0.0",
}
```
"""

OAREPO_ROCRATE_BAGIT_PROFILE: dict[str, Any] | None = None
"""The RO-Crate profile the BagIt export declares, in addition to `OAREPO_ROCRATE_PROFILE`; `None` adds none.

When set, the crate inside the bag declares `conformsTo` this profile, next to `OAREPO_ROCRATE_PROFILE`
if that is set as well, and the profile entity is added to the crate. The `@id` must resolve to the
published profile description - set it only then.
"""

OAREPO_ROCRATE_BAGIT_INFO: dict[str, str] = {}
"""Constant `bag-info.txt` fields of the deployment, e.g. the organization that transfers the bags.

Written after the fields the export derives from the record; a label the export writes is not repeated.

```python
OAREPO_ROCRATE_BAGIT_INFO = {
    "Source-Organization": "Masaryk University",
    "Contact-Email": "data@example.org",
}
```
"""

OAREPO_ROCRATE_AIP_COLLECTORS: list[str] = []
"""Additional collectors of the RO-Crate AIP export, as `module:function` import paths.

They run after the default ones (see [oarepo_formats.rocrate.exports.aip][]) and add the members that only the
model or the deployment can know, e.g. reviews of a custom service.

```python
OAREPO_ROCRATE_AIP_COLLECTORS = ["my_repository.aip:collect_reviews"]
```
"""

OAREPO_ROCRATE_IMPORT_MAX_FILES = 100
"""Refuse an imported RO-Crate with more file entities than this."""

OAREPO_ROCRATE_IMPORT_FILES = True
"""Whether the RO-Crate import adds the crate's files; `False` imports the metadata only.

It is also the way to import into a model that has no files: such a model refuses a crate with files.
"""

OAREPO_ROCRATE_IMPORT_ARCHIVE = True
"""Whether attached packages are accepted: a ZIP in the body, or a URL to one (`application/json`)."""

OAREPO_ROCRATE_IMPORT_MAX_ARCHIVE_SIZE = 100 * 1024 * 1024
"""Largest package accepted in the request body (a larger one belongs behind a URL)."""

OAREPO_ROCRATE_IMPORT_ARCHIVE_TIMEOUT = 30
"""Connect and read timeout of the reads of an archive behind a URL and of downloaded files, in seconds."""

OAREPO_ROCRATE_IMPORT_MAX_FILE_SIZE = 50 * 1024**3
"""Largest file the import downloads, in bytes (50 GB). A file whose crate or server states a larger size is
refused; one whose size nobody states is cut off at this size - a server must not be able to fill the
bucket. The model's own limits (the bucket's quota and maximum file size) apply as well."""

OAREPO_ROCRATE_IMPORT_VERIFY = "strict"
"""What a member whose md5 (the checksum stored at upload) mismatches does: `strict` fails it, `log`
imports it and logs. A sha512 mismatch (the bytes changed after the export) always fails the member."""

OAREPO_ROCRATE_IMPORT_METADATA = "crate"
"""Where an attached package's metadata comes from: `crate` (the RO-Crate, converted to DataCite) or
`record-json` (the package's own `metadata/record.json`, lossless for the same model)."""

OAREPO_ROCRATE_IMPORT_REFERENCED_FILES = "fetch"
"""What the import does with a referenced file - every file of a detached crate, a web-based `File` of an
attached package: `fetch` downloads it into the repository (its domain must be in
`RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS`, and its size and md5 are verified against the crate); `reference`
keeps it a remote file (an `R` transfer) when its domain is in `RECORDS_RESOURCES_FILES_ALLOWED_REMOTE_DOMAINS`,
and downloads it otherwise. `?files=` overrides it for one request."""
