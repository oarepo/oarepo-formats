# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of converting a crate's root data entity back to DataCite JSON.

The round trip (DataCite -> the export's crate -> this conversion) is pinned as a whole by
`data/datacite_round_trip.json`: everything the export keeps, and - by comparing that file with
`data/datacite_full.json` - everything it loses (`lang`, `schemeUri`, title and description types, the
date types beyond Issued/Created/Updated, ...). After an intended change of either mapping, review the
diff and regenerate the file:

```bash
.venv/bin/python -m tests.rocrate.imports.test_crate_datacite
```

The table below covers only what our own export never writes, i.e. what a foreign crate may.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oarepo_formats.rocrate.exports.graph import flatten
from oarepo_formats.rocrate.exports.schema import DOI_REGISTRY, DataCiteToROCrateSchema
from oarepo_formats.rocrate.imports.crate import ROCrate
from oarepo_formats.rocrate.imports.crate_datacite import ROCrateToDataCiteSchema

DATA = Path(__file__).parents[1] / "data"
REFERENCE = DATA / "datacite_round_trip.json"
DOI = "https://doi.org/10.82433/b09z-4k37"
ROR = "https://ror.org/02kkvpp62"


def convert(root: dict) -> dict:
    return ROCrateToDataCiteSchema().dump(root)


def round_trip() -> dict:
    """Return the full DataCite example, exported to a crate and converted back."""
    datacite = json.loads((DATA / "datacite_full.json").read_text())
    tree = DataCiteToROCrateSchema().dump({"datacite": datacite, "record": {"links": {}, "files": {"entries": {}}}})
    return convert(ROCrate(flatten(tree)).root)


def test_the_round_trip_through_the_export():
    """The export's crate, flattened and read back, gives exactly the pinned DataCite document."""
    assert round_trip() == json.loads(REFERENCE.read_text())


@pytest.mark.parametrize(
    ("root", "key", "expected"),
    [
        pytest.param(
            {"@id": "./", "@type": "Dataset"}, "types", {"resourceTypeGeneral": "Dataset"}, id="plain-dataset"
        ),
        pytest.param(
            {"@type": ["Dataset", "Software"]},
            "types",
            {"schemaOrg": "Software", "resourceTypeGeneral": "Dataset"},
            id="schema-org-type",
        ),
        pytest.param(
            {"identifier": [{"@type": "PropertyValue", "propertyID": DOI_REGISTRY, "value": "doi:10.1/x"}]},
            "doi",
            "10.1/x",
            id="doi-from-a-property-value",
        ),
        pytest.param({"@id": DOI}, "doi", "10.82433/b09z-4k37", id="doi-from-the-root-id"),
        pytest.param(
            {"@id": "https://repo.example.org/r/1", "cite-as": DOI}, "doi", "10.82433/b09z-4k37", id="cite-as"
        ),
        pytest.param({"@id": "https://repo.example.org/r/1"}, "doi", None, id="a-landing-page-is-not-a-doi"),
        pytest.param(
            {"publisher": "Example Publisher"}, "publisher", {"name": "Example Publisher"}, id="publisher-text"
        ),
        pytest.param({"author": [{"@type": "Person"}]}, "creators", None, id="a-nameless-agent-is-dropped"),
        pytest.param(
            {"author": [{"@id": ROR, "@type": "Organization", "name": "Example University", "givenName": "x"}]},
            "creators",
            [
                {
                    "name": "Example University",
                    "nameType": "Organizational",
                    "nameIdentifiers": [{"nameIdentifier": "02kkvpp62", "nameIdentifierScheme": "ROR"}],
                }
            ],
            id="an-organization-has-no-given-name",
        ),
        pytest.param(
            {
                "keywords": ["curation", "preservation"],
                "about": [
                    {
                        "@id": "https://example.org/term/1",
                        "@type": "DefinedTerm",
                        "name": "curation",
                        "inDefinedTermSet": "https://example.org/scheme",
                    }
                ],
            },
            "subjects",
            [
                {
                    "subject": "curation",
                    "valueUri": "https://example.org/term/1",
                    "schemeUri": "https://example.org/scheme",
                },
                {"subject": "preservation"},
            ],
            id="a-defined-term-enriches-its-keyword",
        ),
        pytest.param(
            {"funder": [{"@type": "Organization", "name": "Another funder"}]},
            "fundingReferences",
            [{"funderName": "Another funder"}],
            id="a-funder-without-a-grant",
        ),
        pytest.param(
            {"isPartOf": [{"@id": "https://doi.org/10.1/part", "@type": "Dataset"}]},
            "relatedIdentifiers",
            [
                {
                    "relatedIdentifier": "10.1/part",
                    "relatedIdentifierType": "DOI",
                    "relationType": "IsPartOf",
                    "resourceTypeGeneral": "Dataset",
                }
            ],
            id="a-related-doi-url-is-bare",
        ),
        pytest.param(
            {"citation": [{"@id": "https://arxiv.org/abs/arXiv:0706.0001", "identifier": "arXiv:0706.0001"}]},
            "relatedIdentifiers",
            [
                {
                    "relatedIdentifier": "arXiv:0706.0001",
                    "relatedIdentifierType": "arXiv",
                    "relationType": "Cites",
                    "resourceTypeGeneral": "Other",
                }
            ],
            id="a-bare-identifier-is-typed-by-its-url",
        ),
        pytest.param(
            {
                "citation": [
                    {
                        "@id": "https://ui.adsabs.harvard.edu/#abs/2011ApJ...737..103S",
                        "identifier": "2011ApJ...737..103S",
                    }
                ]
            },
            "relatedIdentifiers",
            [
                {
                    "relatedIdentifier": "2011ApJ...737..103S",
                    "relatedIdentifierType": "bibcode",
                    "relationType": "Cites",
                    "resourceTypeGeneral": "Other",
                }
            ],
            id="a-datacite-type-idutils-names-differently",
        ),
        pytest.param(
            {"citation": [{"@id": "https://example.org/works/1234", "identifier": "1234"}]},
            "relatedIdentifiers",
            [
                {
                    "relatedIdentifier": "1234",
                    "relatedIdentifierType": "Other",
                    "relationType": "Cites",
                    "resourceTypeGeneral": "Other",
                }
            ],
            id="a-bare-identifier-not-matching-its-url-is-other",
        ),
    ],
)
def test_a_foreign_crate_property_maps_to_datacite(root, key, expected):
    assert convert(root).get(key) == expected


if __name__ == "__main__":
    REFERENCE.write_text(json.dumps(round_trip(), indent=2, ensure_ascii=False) + "\n")
