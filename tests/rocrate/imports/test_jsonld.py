# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of reading crates other tools write: prefixed and full IRIs, value objects, a crate's own context.

`data/rocrate-1.3-context-terms.json` is an excerpt of the published RO-Crate 1.3 context: the terms the
import reads, with their IRIs. It pins the import's term table to what RO-Crate actually means by them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from marshmallow import ValidationError

from oarepo_formats.files import CHECKSUM_TERMS
from oarepo_formats.rocrate.imports.crate import CrateFile, ROCrate
from oarepo_formats.rocrate.imports.crate_datacite import ROCrateToDataCiteSchema
from oarepo_formats.rocrate.imports.jsonld import (
    SCHEMA_ORG,
    SCHEMA_TERMS,
    TERM_IRIS,
    check_context,
    literal,
    normalize,
    term,
    text,
)

CONTEXT_TERMS = json.loads((Path(__file__).parents[1] / "data" / "rocrate-1.3-context-terms.json").read_text())
RO_CRATE_CONTEXT = "https://w3id.org/ro/crate/1.3/context"


def test_the_term_table_means_what_the_ro_crate_context_means():
    terms = CONTEXT_TERMS["terms"]
    read = (SCHEMA_TERMS | set(TERM_IRIS)) - set(CHECKSUM_TERMS)  # the checksum terms are not RO-Crate's
    assert set(terms) == read
    assert {name: SCHEMA_ORG + name for name in SCHEMA_TERMS} | TERM_IRIS == {**terms, **CHECKSUM_TERMS}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("A title", "A title", id="plain"),
        pytest.param({"@value": "Název", "@language": "cs"}, "Název", id="value-object"),
        pytest.param({"@value": "  "}, None, id="blank-value-object"),
        pytest.param({"@id": "https://x.org"}, None, id="a-reference-is-no-text"),
    ],
)
def test_text_reads_a_value_object(value, expected):
    assert text(value) == expected


def test_literal_unwraps_a_value_object():
    assert literal({"@value": 1234, "@type": "xsd:integer"}) == 1234
    assert literal(1234) == 1234


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("name", "name", id="term"),
        pytest.param("schema:name", "name", id="compact-iri"),
        pytest.param("http://schema.org/name", "name", id="schema-org-iri"),
        pytest.param("https://schema.org/name", "name", id="schema-org-https-iri"),
        pytest.param("http://purl.org/dc/terms/conformsTo", "conformsTo", id="other-vocabulary"),
        pytest.param("dct:conformsTo", "conformsTo", id="other-vocabulary-compact"),
        pytest.param("https://example.org/terms#photoTitle", "https://example.org/terms#photoTitle", id="unknown"),
        pytest.param("@id", "@id", id="keyword"),
    ],
)
def test_term(name, expected):
    assert term(name) == expected


def test_normalize_writes_names_and_types_as_terms():
    entity = {
        "@id": "./",
        "@type": ["schema:Dataset", "https://schema.org/SoftwareSourceCode"],
        "https://schema.org/name": {"@value": "IRI name"},
        "name": "Term name",
        "schema:author": [{"@id": "#jane"}],
        "spatialCoverage": {"@type": "schema:Place", "schema:name": "Brno"},
    }

    assert normalize(entity) == {
        "@id": "./",
        "@type": ["Dataset", "SoftwareSourceCode"],
        "name": "Term name",  # the term wins over an IRI of it
        "author": [{"@id": "#jane"}],
        "spatialCoverage": {"@type": "Place", "name": "Brno"},
    }


@pytest.mark.parametrize(
    "context",
    [
        pytest.param(None, id="none"),
        pytest.param(RO_CRATE_CONTEXT, id="the-ro-crate-context"),
        pytest.param("https://example.org/some/other/context", id="an-unknown-url-is-not-fetched"),
        pytest.param([RO_CRATE_CONTEXT, {"md5": CHECKSUM_TERMS["md5"]}], id="this-packages-export"),
        pytest.param([RO_CRATE_CONTEXT, {"photoTitle": "https://example.org/terms#photoTitle"}], id="an-addition"),
        pytest.param({"name": "schema:name", "author": {"@id": "https://schema.org/author"}}, id="the-same-meaning"),
        pytest.param({"author": {"@type": "@id"}}, id="a-type-coercion"),
        pytest.param({"ex": "https://example.org/", "conformsTo": "dct:conformsTo"}, id="known-prefix"),
    ],
)
def test_a_context_that_keeps_the_meaning_is_accepted(context):
    check_context(context)


@pytest.mark.parametrize(
    ("context", "redefined"),
    [
        pytest.param({"name": "https://example.org/terms#name"}, "name", id="redefined"),
        pytest.param(
            [RO_CRATE_CONTEXT, {"author": {"@id": "ex:creator"}, "ex": "https://ex.org/"}], "author", id="prefixed"
        ),
        pytest.param({"hasPart": None}, "hasPart", id="removed"),
        pytest.param({"localPath": "schema:name"}, "localPath", id="another-term"),
    ],
)
def test_a_context_that_changes_the_meaning_of_a_term_is_refused(context, redefined):
    with pytest.raises(ValidationError, match=f"defines {redefined!r}"):
        check_context(context)


def foreign_crate(**context: object) -> dict:
    """Return a crate as another tool (a lab notebook, say) writes it: IRIs, prefixes, value objects."""
    return {
        "@context": [RO_CRATE_CONTEXT, context] if context else RO_CRATE_CONTEXT,
        "@graph": [
            {
                "@id": "ro-crate-metadata.json",
                "@type": "schema:CreativeWork",
                "http://purl.org/dc/terms/conformsTo": {"@id": "https://w3id.org/ro/crate/1.3"},
                "schema:about": {"@id": "./"},
            },
            {
                "@id": "./",
                "@type": "https://schema.org/Dataset",
                "schema:name": {"@value": "Experiment 42", "@language": "en"},
                "https://schema.org/datePublished": "2026-10-30",
                "schema:hasPart": [{"@id": "https://eln.example.org/files/plate.csv"}],
            },
            {
                "@id": "https://eln.example.org/files/plate.csv",
                "@type": "schema:MediaObject",
                "schema:name": "plate.csv",
                "schema:contentSize": {"@value": "1234"},
            },
        ],
    }


def test_a_foreign_crate_reads_like_one_of_ours():
    crate = ROCrate(foreign_crate())

    assert crate.root["name"] == {"@value": "Experiment 42", "@language": "en"}
    assert crate.files == (CrateFile(key="plate.csv", url="https://eln.example.org/files/plate.csv", size=1234),)
    datacite = ROCrateToDataCiteSchema().dump(crate.root)
    assert datacite["titles"] == [{"title": "Experiment 42"}]
    assert datacite["publicationYear"] == 2026


def test_a_crate_whose_context_changes_a_term_is_refused_by_the_reader():
    with pytest.raises(ValidationError, match="defines 'name'"):
        ROCrate(foreign_crate(name="https://eln.example.org/terms#sampleName"))
