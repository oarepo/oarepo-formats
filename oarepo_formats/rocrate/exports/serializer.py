# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Serializer producing a detached RO-Crate metadata document from a serialized record.

[AddROCrateExport][oarepo_formats.rocrate.model.AddROCrateExport] creates the serializer, so it is normally
not used directly. Subclass it only for changes outside the root data entity, which the schema can not
express - e.g. the license of the metadata itself on the `ro-crate-metadata.json` descriptor
(spec 8.9.1):

```python
CC0 = "https://creativecommons.org/publicdomain/zero/1.0/"


class MySerializer(ROCrateSerializer):
    def dump_obj(self, obj):
        document = super().dump_obj(obj)
        descriptor = next(
            e
            for e in document["@graph"]
            if e["@id"]
            == "ro-crate-metadata.json"
        )
        descriptor["license"] = {
            "@id": CC0
        }
        document["@graph"].append(
            {
                "@id": CC0,
                "@type": "CreativeWork",
                "name": "CC0 1.0",
            }
        )
        return document
```

and register it with `AddMetadataExport` (`AddROCrateExport` does not take a serializer):

```python
AddMetadataExport(
    code="rocrate",
    name=_("RO-Crate"),
    mimetype='application/ld+json; profile="https://w3id.org/ro/crate"',
    extension=".json",
    serializer=MySerializer(),
)
```
"""

from __future__ import annotations

from flask_resources.serializers import JSONSerializer, MarshmallowSerializer
from oarepo_runtime.api import ExportEngine, ExportRepresentation

from ...datacite import DATACITE_JSON_MIMETYPE
from ...files import add_checksum_terms
from .graph import flatten
from .schema import DataCiteToROCrateSchema


class ROCrateSerializer(MarshmallowSerializer):
    """Serialize a record to a detached RO-Crate metadata document (JSON-LD).

    1. The record is exported to DataCite JSON by the DataCite export registered on its model. This
       goes through `ExportEngine`, so in an export cache scope the DataCite export is shared with
       other exports of the same record.
    2. `schema_cls` dumps `{"datacite": ..., "record": ...}` to nested schema.org JSON-LD.
    3. [flatten][oarepo_formats.rocrate.exports.graph.flatten] turns it into the RO-Crate metadata document.

    A search result is serialized as a JSON array of crates, one per hit.
    """

    def __init__(self, schema_cls: type[DataCiteToROCrateSchema] = DataCiteToROCrateSchema):
        """Create the serializer with `DataCiteToROCrateSchema` or a subclass of it."""
        super().__init__(format_serializer_cls=JSONSerializer, object_schema_cls=schema_cls)

    def dump_obj(self, obj: dict) -> dict:
        """Dump a serialized record to the RO-Crate metadata document.

        Args:
            obj: the serialized record (API JSON).

        Returns:
            `{"@context": ..., "@graph": [...]}`; `@graph[0]` is the `ro-crate-metadata.json`
            descriptor, `@graph[1]` the root data entity.

        Raises:
            ValueError: if the record's model has no DataCite JSON export.

        """
        datacite = ExportEngine.export(
            obj,
            export_mimetype=DATACITE_JSON_MIMETYPE,
            representation=ExportRepresentation.DICTIONARY,
        )
        return add_checksum_terms(flatten(self.object_schema.dump({"datacite": datacite, "record": obj})))

    def dump_list(self, obj_list: dict) -> list[dict]:
        """Dump a serialized search result (`{"hits": {"hits": [...]}}`) as a list of crates, one per hit."""
        return [self.dump_obj(hit) for hit in obj_list["hits"]["hits"]]
