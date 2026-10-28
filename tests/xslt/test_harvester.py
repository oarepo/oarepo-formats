# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import Flask
from invenio_vocabularies import config as vocabularies_config
from invenio_vocabularies.datastreams.datastreams import StreamEntry
from invenio_vocabularies.datastreams.errors import TransformerError
from invenio_vocabularies.datastreams.factories import TransformerFactory
from lxml import etree

from oarepo_formats.xslt.harvester import XSLTTransformer, register_harvester_transformer
from tests.xslt.conftest import xsl

OAI = "http://www.openarchives.org/OAI/2.0/"
FIXUP = xsl(
    '<xsl:param name="suffix" as="xs:string" select="\'\'"/>'
    '<xsl:template match="/*"><fixed>{string(.) || $suffix}</fixed></xsl:template>'
)


def oai_entry(metadata: str, deleted=False) -> StreamEntry:
    xml = etree.fromstring(f'<record xmlns="{OAI}"><header/><metadata>{metadata}</metadata></record>')
    record = SimpleNamespace(xml=xml, header=SimpleNamespace(deleted=deleted))
    return StreamEntry({"record": record})


def metadata_of(entry: StreamEntry) -> tuple[str, str]:
    (child,) = entry.entry["record"].xml.find(f"{{{OAI}}}metadata")
    return child.tag, child.text


def test_registered_as_datastream_transformer(stylesheet_file):
    """`xslt{...}` in harvester YAML resolves, without losing the transformers of invenio-vocabularies.

    The extension may be initialized before invenio-vocabularies sets its own defaults.
    """
    app = Flask("test")
    register_harvester_transformer(app)
    assert set(vocabularies_config.VOCABULARIES_DATASTREAM_TRANSFORMERS) < set(
        app.config["VOCABULARIES_DATASTREAM_TRANSFORMERS"]
    )
    transformer = TransformerFactory.create({"type": "xslt", "args": {"stylesheet": stylesheet_file(FIXUP)}})
    assert isinstance(transformer, XSLTTransformer)


def test_metadata_is_replaced(stylesheet_file):
    transformer = XSLTTransformer(stylesheet_file(FIXUP), suffix="!")
    entry = transformer.apply(oai_entry('<dc xmlns="urn:dc">hello</dc>'))
    assert metadata_of(entry) == ("fixed", "hello!")


def test_deleted_record_passes_through(stylesheet_file):
    entry = oai_entry('<dc xmlns="urn:dc">hello</dc>', deleted=True)
    assert metadata_of(XSLTTransformer(stylesheet_file(FIXUP)).apply(entry)) == ("{urn:dc}dc", "hello")


def test_transform_error_is_transformer_error(stylesheet_file):
    """DataStream records TransformerError on the entry; anything else would abort the whole harvest."""
    failing = stylesheet_file(xsl('<xsl:template match="/"><xsl:value-of select="error()"/></xsl:template>'))
    with pytest.raises(TransformerError, match="Error signalled by application"):
        XSLTTransformer(failing).apply(oai_entry("<dc/>"))
