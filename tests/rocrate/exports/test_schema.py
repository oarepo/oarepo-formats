# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of the DataCite to RO-Crate mapping (dumped and flattened).

The mapping of every property is pinned by `data/rocrate_full.json`, the crate of DataCite's full
example record `data/datacite_full.json`. After an intended change of the mapping, review the diff and
regenerate the file:

```bash
.venv/bin/python -m tests.rocrate.exports.test_schema
```
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from marshmallow import fields

from oarepo_formats.rocrate.exports.graph import flatten
from oarepo_formats.rocrate.exports.schema import DataCiteToROCrateSchema
from tests.rocrate.documents import descriptor_of, root_of

DATA = Path(__file__).parents[1] / "data"
FULL = json.loads((DATA / "datacite_full.json").read_text())
REFERENCE = DATA / "rocrate_full.json"


def convert(datacite: dict, record: dict | None = None, schema: DataCiteToROCrateSchema | None = None) -> dict:
    """Return the flattened RO-Crate document."""
    return flatten((schema or DataCiteToROCrateSchema()).dump({"datacite": datacite, "record": record or {}}))


def full_crate() -> dict:
    """Return the crate of the full DataCite example."""
    return convert(FULL, {"links": {"self_html": "https://repo/r/1"}})


def by_id(document: dict) -> dict[str, dict]:
    return {e["@id"]: e for e in document["@graph"]}


def test_full_datacite_matches_reference():
    assert full_crate() == json.loads(REFERENCE.read_text())


def test_full_datacite_satisfies_spec_requirements():
    """MUST/SHOULD requirements of spec 4.2 and 6.2 on a fully populated DataCite record."""
    document = full_crate()
    graph = by_id(document)

    assert document["@context"] == "https://w3id.org/ro/crate/1.3/context"
    descriptor = graph["ro-crate-metadata.json"]
    assert descriptor["conformsTo"] == {"@id": "https://w3id.org/ro/crate/1.3"}
    root = graph[descriptor["about"]["@id"]]
    assert root["@id"] == "https://doi.org/10.82433/b09z-4k37"
    assert "Dataset" in ([root["@type"]] if isinstance(root["@type"], str) else root["@type"])
    for prop in ("name", "description", "datePublished", "license"):
        assert root.get(prop), prop
    # flattened: every entity is described once, no nested objects, every reference resolves
    assert len(graph) == len(document["@graph"])

    def references(value: Any):
        if isinstance(value, dict):
            assert set(value) == {"@id"}, f"nested object {value}"
            yield value["@id"]
        elif isinstance(value, list):
            for v in value:
                yield from references(v)

    for entity in document["@graph"]:
        for key, value in entity.items():
            if key == "@id":
                continue
            for target in references(value):
                assert key == "conformsTo" or target in graph, f"{entity['@id']} references undescribed {target}"


@pytest.mark.parametrize(
    ("datacite", "record", "expected"),
    [
        ({"doi": "10.1/x"}, {"links": {"self_html": "https://repo/r/1"}}, "https://doi.org/10.1/x"),
        ({"identifiers": [{"identifier": "10.1/x", "identifierType": "DOI"}]}, {}, "https://doi.org/10.1/x"),
        ({}, {"links": {"self_html": "https://repo/r/1"}}, "https://repo/r/1"),
        ({}, {}, "./"),
    ],
    ids=["doi", "doi-in-identifiers", "landing-page", "neither"],
)
def test_root_id(datacite, record, expected):
    """The root is the DOI if there is one, otherwise the landing page."""
    document = convert(datacite, record)
    descriptor, root = descriptor_of(document), root_of(document)
    assert root["@id"] == expected
    assert descriptor["about"] == {"@id": expected}


def test_person_listed_twice_is_one_entity():
    """A creator who is also a contributor is described once and referenced from both properties."""
    jane = {
        "name": "Doe, Jane",
        "nameType": "Personal",
        "nameIdentifiers": [{"nameIdentifier": "0000-0002-1825-0097", "nameIdentifierScheme": "ORCID"}],
    }
    document = convert({"creators": [jane], "contributors": [{**jane, "contributorType": "ContactPerson"}]})

    root = root_of(document)
    assert root["author"] == root["contributor"] == [{"@id": "https://orcid.org/0000-0002-1825-0097"}]
    assert sum(e["@id"] == "https://orcid.org/0000-0002-1825-0097" for e in document["@graph"]) == 1


def test_schema_can_be_customized():
    """Mappings are changed and added the marshmallow way: subclass and override or add a field."""

    class Schema(DataCiteToROCrateSchema):
        author = fields.Constant(None, dump_only=True)  # drop a mapping
        creditText = fields.Method("get_credit_text")  # noqa: N815 add a property

        def get_name(self, obj):  # change a mapping
            return obj["datacite"]["titles"][0]["title"].upper()

        def get_credit_text(self, obj):
            return f"{obj['datacite']['titles'][0]['title']} ({obj['record']['id']})"

    document = convert({"titles": [{"title": "t"}], "creators": [{"name": "A"}]}, {"id": "abc"}, schema=Schema())

    root = root_of(document)
    assert root["name"] == "T"
    assert root["creditText"] == "t (abc)"
    assert "author" not in root


if __name__ == "__main__":
    REFERENCE.write_text(json.dumps(full_crate(), indent=2) + "\n")
