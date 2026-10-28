# XSLT exports and imports

This page is part of [oarepo-formats](index.md), which adds import and export formats to the records
of [oarepo-model](https://github.com/oarepo/oarepo-model) based Invenio repositories.

The **XSLT exports and imports** (`oarepo_formats.xslt`) add a new XML format to a model without
writing a serializer: an [XSLT 3.0](https://www.w3.org/TR/xslt-30/) stylesheet converts the output
of an export the model already has, or converts an uploaded document into a format an existing import
understands. Once the `AddXSLTExport` / `AddXSLTImport` customizations are added to a model, the new
format is offered on the record's landing page, served and accepted by the REST API, and usable by the
OAI-PMH server and harvester. Stylesheets run in a sandbox (SaxonC-HE in isolated worker processes),
as they are treated as untrusted code.

| Customization | What it adds |
|---|---|
| **`AddXSLTExport`** | a new export: the output of an export the model already has, converted by a stylesheet - served by the REST API and the OAI-PMH server |
| **`AddXSLTImport`** | a new import: the uploaded document is converted into what an import the model already has understands - accepted by the REST API and the OAI-PMH harvester |

The export or import it builds on may itself be an XSLT one, so formats chain. A stylesheet can live in a
Python package or at a `https:` URL, and a deployment can point any of them elsewhere in
`invenio.cfg`; `invenio oarepo xslt list|show|refresh|test` lists the stylesheets, downloads and
compiles them up front, and runs a test transformation. Harvests use the same machinery through the
`xslt` datastream transformer, which rewrites the OAI `<metadata>` before `oai-import`.

The full reference (usage, stylesheet locations, sandbox, configuration) is in the API documentation
of `oarepo_formats.xslt` and its modules.

## Usage

Build a new version of a format on the export and import the
[oarepo-model](https://github.com/oarepo/oarepo-model) model already has:

```python
from oarepo_formats.xslt import AddXSLTExport, AddXSLTImport

customizations = [
    AddMetadataExport(code="ccmm-xml", ..., serializer=CCMMXMLSerializer()),
    AddMetadataImport(code="ccmm-xml", ..., deserializer=CCMMXMLDeserializer()),
    AddXSLTExport(
        code="ccmm-xml-2",
        name=_("CCMM 2.0 XML"),
        mimetype="application/x-ccmm-2+xml",
        stylesheet="ccmm_invenio.xslt:ccmm-1.1.0-to-2.0.0.xslt",
        base_export="ccmm-xml",  # its output is transformed
    ),
    AddXSLTImport(
        code="ccmm-xml-2",
        name=_("CCMM 2.0 XML"),
        description=_("CCMM 2.0 XML document"),
        mimetype="application/x-ccmm-2+xml",
        stylesheet="ccmm_invenio.xslt:ccmm-2.0.0-to-1.1.0.xslt",
        base_import="ccmm-xml",  # gets the transformed body
    ),
]
```

If the source format is not an export or import of the model, pass `serializer=` / `deserializer=`
instead of `base_export` / `base_import`.

Point a stylesheet elsewhere at deployment, and download and compile all stylesheets up front:

```python
# invenio.cfg
OAREPO_XSLT_STYLESHEETS = {
    "import:ccmm-xml-2": "https://raw.githubusercontent.com/techlib/CCMM-utils/v2.0.1/xslt/ccmm-2.0.0-to-1.1.0.xslt",
}
```

```bash
invenio oarepo xslt refresh
```

Stylesheets are treated as untrusted code: they cannot read files, reach the network or see the
environment, and are killed after `OAREPO_XSLT_TIMEOUT` seconds.

## Tests

The sandbox, stylesheet loading and harvester tests (`tests/xslt`) need no docker services, so run
them with `./run.sh test --skip-services tests/xslt`. The REST export and import through a real model
are in `tests/api_tests/test_xslt.py` and need the services (`./run.sh test`).
