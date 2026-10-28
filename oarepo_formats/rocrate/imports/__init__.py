# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""The RO-Crate import: create a record from an RO-Crate, with its files.

[AddROCrateImport][oarepo_formats.rocrate.model.AddROCrateImport] mixes
[ROCrateImportResourceMixin][oarepo_formats.rocrate.imports.resource.ROCrateImportResourceMixin] into the
model's generated `RecordResource`, which gives every model of the deployment

```text
POST <model url_prefix>/import/rocrate[?publish][&notifications=off][&metadata=crate|record-json]
    [&files=fetch|reference]
Content-Type: application/ld+json      # a detached crate
Content-Type: application/zip          # an attached package in the body
Content-Type: application/json         # {"url": "https://..."}: an attached package behind a URL
```

| Input | Files come from | When |
|---|---|---|
| detached crate | their URLs, downloaded | `import_archive`, after the response |
| attached package in the body | the package, and its referenced files | in the request, then a task |
| attached package behind a URL | the package, by HTTP ranges | `import_archive`, after the response |

One task writes whatever is left after the response: [oarepo_formats.rocrate.imports.tasks][].

The body is read by [oarepo_formats.rocrate.imports.crate][] for a crate and by
[oarepo_formats.rocrate.imports.archive][] for a package - a plain attached crate or a BagIt bag, told
apart by the archive's own table of contents. The metadata is converted to DataCite JSON
([oarepo_formats.rocrate.imports.crate_datacite][]) and handed to the model's own data import (which the
deployment writes) - or, by a schema of the deployment's, to the record's own structure, handed to the
identity import (`IDENTITY_IMPORT`) - or, with `?metadata=record-json`, taken from the package's own
`record.json`. Either
way the record is created and its files are planned in **one unit of work**, so an invalid file plan, a
member that does not match its checksum, or a missing permission leaves nothing behind; the outcome
reaches the uploader by mail ([oarepo_formats.rocrate.imports.notifications][]).

The route reuses three rules of the export side, rather than copying them: the file keys are checked with
[check_file_key][oarepo_formats.rocrate.exports.zip.check_file_key] (an archive path must not escape its
root, and a package's names are untrusted input too), the checksum properties are read from the terms
[CHECKSUM_TERMS][oarepo_formats.files.CHECKSUM_TERMS] defines, and the fixity of a package is read the way
the bag export writes it: the bag's sha512 is computed from the bytes, so a mismatch always fails the file;
the md5 is the checksum stored at upload, so a mismatch fails the file under
`OAREPO_ROCRATE_IMPORT_VERIFY = "strict"` (the default) and is only logged under `"log"`.

The deployment's prerequisites (a DataCite import and the allow-list) are in the import section of
`docs/rocrate.md`.
"""

from __future__ import annotations

from .resource import IDENTITY_IMPORT as IDENTITY_IMPORT
from .resource import ROUTE as ROUTE
from .resource import ROCrateImportConfig as ROCrateImportConfig
from .resource import ROCrateImportResourceMixin as ROCrateImportResourceMixin
