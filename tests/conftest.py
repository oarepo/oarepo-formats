# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""The model of the integration tests (`api_tests`), shared by all formats.

It is created session-scoped and only here, otherwise sqlalchemy would try to map it multiple times.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from flask_resources.deserializers import DeserializerMixin
from flask_resources.serializers import BaseSerializer
from lxml import etree
from oarepo_runtime.services.config import EveryonePermissionPolicy

from oarepo_formats.datacite import DATACITE_JSON_MIMETYPE

DATACITE = json.loads((Path(__file__).parent / "rocrate" / "data" / "datacite_full.json").read_text())

DATASET_XML_MIMETYPE = "application/x-dataset+xml"
"""The XSLT export and import: `<dataset><name>`, converted from/to the model's own `<record><title>` XML."""

DATASET_V2_XML_MIMETYPE = "application/x-dataset-v2+xml"
"""XSLT export and import built on top of the `dataset-xml` ones: `<dataset version="2"><title>`."""

DATASET_V2_SCHEMA = "https://example.org/schemas/dataset-v2.xsd"
"""The schema the dataset-v2-xml export declares; the RO-Crate ZIP uses it as `conformsTo`."""

DATASET_V2_ZIP_PATH = "metadata/dataset-v2.xml"
"""Archive path of the dataset-v2-xml export in the RO-Crate ZIP."""


class FormatsTestPermissionPolicy(EveryonePermissionPolicy):
    """The test model's policy: everyone.

    The import needs nothing beyond it, not even for its asynchronous part: the task writes the files as
    the importing user, in their own draft.
    """


class DataCiteSerializer(BaseSerializer):
    """DataCite JSON serializer stub returning the full DataCite fixture."""

    def __init__(self):
        """Create the stub with a call counter."""
        self.calls = 0

    def serialize_object(self, obj):
        """Return the DataCite fixture as JSON."""
        self.calls += 1
        return json.dumps(DATACITE)


class DataCiteDeserializer(DeserializerMixin):
    """The inverse of `DataCiteSerializer`: the crate's DataCite JSON back to the model's metadata.

    A deployment writes its own; this stub takes the title and the abstract, of which the test model keeps
    the title (it has no description field, so the abstract is dropped by the model).
    """

    def deserialize(self, data):
        """Deserialize a DataCite attributes object to the model's metadata."""
        datacite = json.loads(data)
        metadata = {"title": datacite["titles"][0]["title"]}
        if description := next(
            (d["description"] for d in datacite.get("descriptions") or [] if d.get("descriptionType") == "Abstract"),
            None,
        ):
            metadata["description"] = description
        return {"metadata": metadata, "files": {"enabled": False}}


class RecordXMLSerializer(BaseSerializer):
    """The model's own XML format, `<record><title>`, that the XSLT export converts from."""

    def serialize_object(self, obj):
        """Serialize the title."""
        record = etree.Element("record")
        etree.SubElement(record, "title").text = obj["metadata"]["title"]
        return etree.tostring(record, encoding=str)


class RecordXMLDeserializer(DeserializerMixin):
    """The model's own XML format, `<record><title>`, that the XSLT import converts to."""

    def deserialize(self, data):
        """Deserialize the title."""
        return {"metadata": {"title": etree.fromstring(data).findtext("title")}, "files": {"enabled": False}}


@pytest.fixture(scope="session")
def formats_model():
    """Build the records model with drafts and files, the RO-Crate and README exports and XSLT exports and imports.

    The RO-Crate and README exports are converted from a DataCite JSON export stub.
    """
    from oarepo_model.api import model
    from oarepo_model.customizations import AddMetadataExport, AddMetadataImport, SetPermissionPolicy
    from oarepo_model.presets.drafts import drafts_preset
    from oarepo_model.presets.records_resources import records_resources_preset

    from oarepo_formats.readme import AddReadmeExport
    from oarepo_formats.rocrate import (
        AddROCrateAIPExport,
        AddROCrateBagItExport,
        AddROCrateExport,
        AddROCrateImport,
        AddROCrateZipExport,
    )
    from oarepo_formats.rocrate.exports.zip import DEFAULT_INCLUDED_EXPORTS
    from oarepo_formats.xslt import AddXSLTExport, AddXSLTImport

    built = model(
        name="formats_test",
        version="1.0.0",
        presets=[records_resources_preset, drafts_preset],
        types=[{"Metadata": {"properties": {"title": {"type": "fulltext+keyword", "required": True}}}}],
        metadata_type="Metadata",
        customizations=[
            SetPermissionPolicy(FormatsTestPermissionPolicy),
            AddMetadataExport(
                code="datacite",
                name="DataCite",
                mimetype=DATACITE_JSON_MIMETYPE,
                serializer=DataCiteSerializer(),
            ),
            # the import's prerequisite: a deployment writes its own DataCite import
            AddMetadataImport(
                code="datacite",
                name="DataCite",
                description="DataCite JSON",
                mimetype=DATACITE_JSON_MIMETYPE,
                deserializer=DataCiteDeserializer(),
            ),
            AddROCrateImport(),
            AddROCrateExport(),
            # the RDM exports of the defaults are not in this model, so the ZIP leaves them out
            AddROCrateZipExport(include_exports={**DEFAULT_INCLUDED_EXPORTS, "dataset-v2-xml": DATASET_V2_ZIP_PATH}),
            AddROCrateBagItExport(
                include_exports={
                    **DEFAULT_INCLUDED_EXPORTS,
                    "dataset-v2-xml": DATASET_V2_ZIP_PATH,
                    "json": "metadata/record.json",
                }
            ),
            AddROCrateAIPExport(
                include_exports={
                    **DEFAULT_INCLUDED_EXPORTS,
                    "dataset-v2-xml": DATASET_V2_ZIP_PATH,
                    "json": "metadata/record.json",
                }
            ),
            AddReadmeExport(),
            AddXSLTExport(
                code="dataset-xml",
                name="Dataset XML",
                mimetype=DATASET_XML_MIMETYPE,
                stylesheet="tests.api_tests.data:record-to-dataset.xslt",
                serializer=RecordXMLSerializer(),
            ),
            AddXSLTExport(
                code="dataset-v2-xml",
                name="Dataset XML v2",
                mimetype=DATASET_V2_XML_MIMETYPE,
                stylesheet="tests.api_tests.data:dataset-to-v2.xslt",
                base_export="dataset-xml",
                oai_schema=DATASET_V2_SCHEMA,
            ),
            # before the import it hands over to: the lookup must not depend on the order of customizations
            AddXSLTImport(
                code="dataset-v2-xml",
                name="Dataset XML v2",
                description="Dataset XML v2",
                mimetype=DATASET_V2_XML_MIMETYPE,
                stylesheet="tests.api_tests.data:v2-to-dataset.xslt",
                base_import="dataset-xml",
            ),
            AddXSLTImport(
                code="dataset-xml",
                name="Dataset XML",
                description="Dataset XML",
                mimetype=DATASET_XML_MIMETYPE,
                stylesheet="tests.api_tests.data:dataset-to-record.xslt",
                deserializer=RecordXMLDeserializer(),
            ),
        ],
    )
    built.register()
    return built
