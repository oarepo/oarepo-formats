# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""XSLT exports and imports of oarepo records: sandboxed XSLT 3.0 transformations of XML.

Adds exports and imports to an oarepo-model model whose format is produced by an XSLT stylesheet.
Stylesheets are run by SaxonC-HE in isolated worker processes. The typical use is a new version or
variant of a format the model already exports or imports, without writing its serializer again:

- [AddXSLTExport][oarepo_formats.xslt.model.AddXSLTExport] - an export that applies a stylesheet to the
  output of another export of the model (REST export, OAI-PMH server),
- [AddXSLTImport][oarepo_formats.xslt.model.AddXSLTImport] - an import that applies a stylesheet to the
  request body and hands the result to another import of the model (REST import, OAI-PMH harvest).

A stylesheet can also be applied to harvested OAI-PMH records by the `xslt` datastream transformer in
[oarepo_formats.xslt.harvester][], or directly to an lxml element by
[XSLTTransform][oarepo_formats.xslt.transform.XSLTTransform].

To use it, import the customizations `AddXSLTExport` and `AddXSLTImport` from `oarepo_formats.xslt`.

## Requirements

`saxonche` is installed as a dependency, but imported only in the sandbox worker processes. The
`https:` stylesheets need a flask-caching cache (`invenio_cache`) shared by all processes.

## Usage

The customizations take the arguments of oarepo-model's `AddMetadataExport` / `AddMetadataImport`, the
default location of the stylesheet and the code of the export or import they build on. Here the model
already has a CCMM 1.1 XML export and import, and gets CCMM 2.0 ones:

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
        base_export="ccmm-xml",
    ),
    AddXSLTImport(
        code="ccmm-xml-2",
        name=_("CCMM 2.0 XML"),
        description=_("CCMM 2.0 XML document"),
        mimetype="application/x-ccmm-2+xml",
        stylesheet="ccmm_invenio.xslt:ccmm-2.0.0-to-1.1.0.xslt",
        base_import="ccmm-xml",
    ),
]
```

The export transforms the output of `base_export`, looked up when a record is exported (in an
export cache scope, the two share the serialized record). The import transforms the request body and
hands the result over to `base_import`. The order of the customizations does not matter, and the
base export or import can be an XSLT one as well, so formats can be chained.

If the format the stylesheet works on is not an export or import of the model, pass its serializer or
deserializer instead - `serializer=CCMMXMLSerializer()` in place of `base_export`,
`deserializer=CCMMXMLDeserializer()` in place of `base_import`. The (de)serializers the customizations
create, [XSLTSerializer][oarepo_formats.xslt.serializers.XSLTSerializer] and
[XSLTDeserializer][oarepo_formats.xslt.serializers.XSLTDeserializer], can also be used directly.

The stylesheets are named `export:<code>` and `import:<code>` (`stylesheet_name` sets another name);
the name is used in the configuration below and in the `invenio oarepo xslt` commands.

Any error (stylesheet does not compile, dynamic error, `xsl:message terminate="yes"`, timeout, empty
result) is raised as `marshmallow.ValidationError`, so the REST API returns 400 and the harvester
records the error on the harvested record.

The OAI-PMH harvester applies a stylesheet with the `xslt` transformer, before `oai-import`:

```yaml
transformer:
  - 'xslt{"stylesheet": "https://example.org/xslt/v1.2/fixups.xslt", "some-param": "value"}'
  - 'oai-import{"model":"datasets"}'
```

## Where stylesheets come from

The default location given in code can be changed per name in `invenio.cfg`, as can the stylesheet
parameters (harvester stylesheets are named `harvester:<location>`):

```python
OAREPO_XSLT_STYLESHEETS = {
    "import:ccmm-xml-2": "https://raw.githubusercontent.com/techlib/CCMM-utils/v2.0.1/xslt/ccmm-2.0.0-to-1.1.0.xslt",
}
OAREPO_XSLT_PARAMS = {
    "export:ccmm-xml-2": {"repository-publisher-ror": "https://ror.org/..."},
}
```

A location is `module:path` (a file in a python package) or an `https:` URL, optionally pinned with
`#sha256=<hex>`. `https:` stylesheets are
stored in the Invenio cache and checked for a new version every `OAREPO_XSLT_REFRESH_INTERVAL` seconds;
a new version replaces the cached one only if it compiles. Run `invenio oarepo xslt refresh` at deploy
time to download and compile everything up front. Details are in [oarepo_formats.xslt.sources][], all
settings in [oarepo_formats.xslt.config][] and the commands in [oarepo_formats.xslt.cli][].

Stylesheets must be self-contained: `xsl:include` / `xsl:import` are refused like any other URI access.

## Sandbox

Stylesheets are treated as untrusted code. They run in separate worker processes (`python -I`, empty
environment, empty working directory) with Saxon's `allowedProtocols` set to nothing, so they cannot
read local files, reach the network, see environment variables or write `xsl:result-document` files.
A transformation running longer than `OAREPO_XSLT_TIMEOUT` seconds kills its worker. Input XML is
parsed with a safe parser (no DTD, no entities) before it reaches Saxon. See [oarepo_formats.xslt.pool][]
and [oarepo_formats.xslt.sandbox][].
"""

from __future__ import annotations

# the API for using the transformations; extension points are imported from the modules. Explicit re-exports
# (PEP 484 `X as X`) not listed in __all__, so the API documentation shows them only on their module page
from .model import AddXSLTExport as AddXSLTExport
from .model import AddXSLTImport as AddXSLTImport
from .serializers import XSLTDeserializer as XSLTDeserializer
from .serializers import XSLTSerializer as XSLTSerializer
from .transform import XSLTTransform as XSLTTransform
