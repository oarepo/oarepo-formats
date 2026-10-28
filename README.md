# OARepo Formats

Format conversions for imports and exports of [OARepo](https://github.com/oarepo) / Invenio repositories.

The package collects support for several formats. Each lives in its own subpackage and has its own
page in the documentation:

| Format | Package | | Documentation |
|---|---|---|---|
| XSLT 3.0 | `oarepo_formats.xslt` | sandboxed XSLT transformations of imported and exported XML (REST API, OAI-PMH harvester and server) | `docs/xslt.md` ("XSLT" page) |
| RO-Crate | `oarepo_formats.rocrate` | export of a record as a JSON-LD RO-Crate, as a streamed ZIP with all its files, or as a streamed zipped BagIt bag (AIP-compatible) | `docs/rocrate.md` ("RO-Crate" page) |
| README | `oarepo_formats.readme` | export of a record as a generated README (`<id>.generated.md`) following the Czech Data Steward Community template; the Jinja template can be overridden | `docs/readme.md` ("README" page) |

## Installation

```bash
pip install oarepo-formats
```

Requires Python 3.14 and Invenio 14.

## Usage

The formats are plugged into a model with [oarepo-model](https://github.com/oarepo/oarepo-model)
customizations, for example:

```python
from oarepo_formats.readme import AddReadmeExport
from oarepo_formats.rocrate import AddROCrateBagItExport, AddROCrateExport, AddROCrateZipExport
from oarepo_formats.xslt import AddXSLTExport

customizations = [
    AddXSLTExport(
        code="ccmm-xml-2",
        name=_("CCMM 2.0 XML"),
        mimetype="application/x-ccmm-2+xml",
        stylesheet="ccmm_invenio.xslt:ccmm-1.1.0-to-2.0.0.xslt",
        base_export="ccmm-xml",  # an export the model already has
    ),
    AddROCrateExport(),
    AddROCrateZipExport(),
    AddROCrateBagItExport(),
    AddReadmeExport(),
]
```

See the format pages for requirements, configuration and customization.

### The record's PID

The README and RO-Crate exports identify the record by its PID - its DOI by default. A repository minting
other PIDs (ePIC handles, ARKs, ...) lists the DataCite `identifierType`s in priority order in
`invenio.cfg`; the first one the record has in its DataCite `identifiers` is used:

```python
OAREPO_FORMATS_PID_SCHEMES = ["DOI", "Handle", "ARK"]
```

The value may also be a callable `(record, datacite) -> list`, choosing the list per record (e.g. per
model).

## Development

```bash
./run.sh venv
./run.sh test                              # everything; the integration tests (tests/api_tests) need the docker services
./run.sh test --skip-services tests/xslt   # the XSLT sandbox tests need none
./run.sh lint
./run.sh docs                              # build the documentation
```

## License

MIT, see [LICENSE](LICENSE).
