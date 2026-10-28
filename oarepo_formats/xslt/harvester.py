# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""`xslt` datastream transformer for the OAI-PMH harvester.

Rewrites the child of the OAI `<metadata>` element in place, before `oai-import`:

```yaml
transformer:
  - 'xslt{"stylesheet": "https://example.org/xslt/v1.2/fixups.xslt"}'
  - 'oai-import{"model":"datasets"}'
```

Extra arguments are passed to the stylesheet as parameters. The stylesheet can be overridden in
`OAREPO_XSLT_STYLESHEETS` under the name `harvester:<stylesheet>`. The transformer is registered
under the name `xslt` by the [OARepoFormats][oarepo_formats.ext.OARepoFormats] extension.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from invenio_vocabularies import config as vocabularies_config
from invenio_vocabularies.datastreams.errors import TransformerError
from invenio_vocabularies.datastreams.transformers import BaseTransformer
from marshmallow import ValidationError

from .transform import HARVESTER_PREFIX, ParamValue, XSLTTransform

if TYPE_CHECKING:
    from flask import Flask

OAI_METADATA = "{http://www.openarchives.org/OAI/2.0/}metadata"


class XSLTTransformer(BaseTransformer):
    """Applies an XSLT to the metadata of a harvested OAI-PMH record."""

    def __init__(self, stylesheet: str, **params: ParamValue):
        """Create the transformer for a stylesheet location."""
        super().__init__()
        self.transform = XSLTTransform(HARVESTER_PREFIX + stylesheet, stylesheet, params)

    def apply(self, stream_entry: Any, *_args: Any, **_kwargs: Any) -> Any:
        """Transform the metadata element of the record in place.

        Raises:
            TransformerError: if `<metadata>` does not have exactly one element or the transformation fails.

        """
        record = stream_entry.entry["record"]
        if record.header.deleted:
            return stream_entry
        metadata = record.xml.find(OAI_METADATA)
        children = [c for c in metadata if isinstance(c.tag, str)] if metadata is not None else []
        if len(children) != 1:
            raise TransformerError("OAI record must have exactly one element inside <metadata>")
        try:
            metadata.replace(children[0], self.transform(children[0]).getroot())
        except ValidationError as e:
            raise TransformerError("; ".join(map(str, e.messages))) from e
        return stream_entry


def register_harvester_transformer(app: Flask) -> None:
    """Register `XSLTTransformer` as the `xslt` datastream transformer, unless the app already has one.

    `VOCABULARIES_DATASTREAM_TRANSFORMERS` is created from invenio-vocabularies' defaults if not set yet
    (this may run before invenio-vocabularies sets its own defaults).
    """
    transformers = app.config.setdefault(
        "VOCABULARIES_DATASTREAM_TRANSFORMERS", {**vocabularies_config.VOCABULARIES_DATASTREAM_TRANSFORMERS}
    )
    transformers.setdefault("xslt", XSLTTransformer)
