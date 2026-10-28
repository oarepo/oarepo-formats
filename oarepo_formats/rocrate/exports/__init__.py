# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""The RO-Crate export pipeline: a record as an RO-Crate document, a ZIP, a bag or an AIP.

| Module | |
|---|---|
| [schema][oarepo_formats.rocrate.exports.schema] | `DataCiteToROCrateSchema` - the DataCite JSON to RO-Crate mapping |
| [serializer][oarepo_formats.rocrate.exports.serializer] | the JSON-LD document of the `rocrate` export |
| [graph][oarepo_formats.rocrate.exports.graph] | flattening of the nested schema.org output into an `@graph` |
| [zip][oarepo_formats.rocrate.exports.zip] | the streamed ZIP (`rocrate-zip`), `CrateContent` and `check_file_key` |
| [bag][oarepo_formats.rocrate.exports.bag] | the BagIt bag over the ZIP's content (`rocrate-bagit`) |
| [aip][oarepo_formats.rocrate.exports.aip] | the Archival Information Package (`rocrate-aip`) and its collectors |
| [provenance][oarepo_formats.rocrate.exports.provenance] | the record's history as RO-Crate actions |

The record's files as `File` entities and the checksum vocabulary are shared with the README and live one
level up, in [oarepo_formats.files][]; the DataCite JSON mimetype and the identifier helpers both formats
use are in [oarepo_formats.datacite][].

The customizations that add the exports to a model are in [oarepo_formats.rocrate.model][]; the usage,
the archive layouts and the configuration are in the docstring of [oarepo_formats.rocrate][] and in
`docs/rocrate.md`.
"""

from __future__ import annotations
