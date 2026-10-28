# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""The JSON-LD shapes the RO-Crate import reads, in one place.

The reader ([crate][oarepo_formats.rocrate.imports.crate]) and the DataCite converter
([crate_datacite][oarepo_formats.rocrate.imports.crate_datacite]) read the same flattened document
(spec 4.2), where a property is a value or a list of values and a reference is `{"@id": ...}`. The helpers
here are the whole vocabulary they need, and the one copy of it: a crate may write `@type` as one string
or as a list, a value may be nested or flat, and a text property may be missing or blank.

## Plain JSON, with the tolerance of a JSON-LD reader

The import does not run a JSON-LD processor: the RO-Crate specification makes the metadata document
readable as plain JSON, and a processor would fetch remote contexts, drop every key its context does not
define and lose the order of arrays (a crate's authors). It reads the keys as the RO-Crate context names
them (`name`, `hasPart`, ...), and accepts what other tools write for the same meaning:

- **prefixed and full IRIs** - `schema:name`, `http://schema.org/name`, `https://schema.org/name`, and the
  full IRIs of the few terms that are not schema.org's (`TERM_IRIS`) - in property names and in `@type`s,
  see [normalize][oarepo_formats.rocrate.imports.jsonld.normalize];
- **value objects** - `{"@value": "...", "@language": "cs"}` where a plain value is expected, see
  [literal][oarepo_formats.rocrate.imports.jsonld.literal].

What plain JSON can not see is a crate whose own `@context` gives a term another meaning; such a crate is
refused rather than misread, see [check_context][oarepo_formats.rocrate.imports.jsonld.check_context].
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from marshmallow import ValidationError

from ...files import CHECKSUM_TERMS

if TYPE_CHECKING:
    from collections.abc import Mapping

SCHEMA_ORG = "http://schema.org/"
"""The schema.org namespace as the RO-Crate context writes it; `https://schema.org/` means the same."""

SCHEMA_TERMS = frozenset(
    {
        "about", "affiliation", "alternateName", "author", "citation", "contentSize", "contentUrl", "contributor",
        "dateCreated", "dateModified", "datePublished", "description", "familyName", "funder", "funding", "geo",
        "givenName", "hasPart", "identifier", "inDefinedTermSet", "inLanguage", "isBasedOn", "isPartOf",
        "keywords", "license", "name", "propertyID", "publisher", "sameAs", "spatialCoverage", "subjectOf",
        "url", "value", "version",
    }
)  # fmt: skip
"""The schema.org terms the import reads; each means `http://schema.org/<term>`, as in the RO-Crate context."""

TERM_IRIS: dict[str, str] = {
    "conformsTo": "http://purl.org/dc/terms/conformsTo",
    "cite-as": "http://www.iana.org/assignments/relation/cite-as",
    "asWKT": "http://www.opengis.net/ont/geosparql#asWKT",
    "localPath": "https://w3id.org/ro/terms#localPath",
    **CHECKSUM_TERMS,  # not in the RO-Crate context: this package's export defines them inline
}
"""The other terms the import reads, with the IRIs the RO-Crate 1.3 context (and the checksum terms) give them."""

PREFIXES = {
    "schema": SCHEMA_ORG,
    "dct": "http://purl.org/dc/terms/",
    "geosparql": "http://www.opengis.net/ont/geosparql#",
    "wrroc": "https://w3id.org/ro/terms/workflow-run#",
}
"""Compact IRI prefixes a crate may use without defining them (the RO-Crate context's own prefixes)."""

_TERM_OF_IRI = {iri: term for term, iri in TERM_IRIS.items()}


def as_list(value: Any) -> list:
    """Return a JSON-LD property value as a list (a missing value is an empty list)."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def types(entity: Mapping[str, Any]) -> set[str]:
    """Return the `@type`s of an entity as a set (a crate may write one or a list)."""
    return {value for value in as_list(entity.get("@type")) if isinstance(value, str)}


def reference_id(value: Any) -> str | None:
    """Return the `@id` of a reference object, or None."""
    if isinstance(value, dict) and isinstance(value.get("@id"), str):
        return value["@id"]
    return None


def literal(value: Any) -> Any:
    """Return the plain value of a JSON-LD value object (`{"@value": ..., "@language": ...}`), else the value."""
    if isinstance(value, dict) and "@value" in value:
        return value["@value"]
    return value


def text(value: Any) -> str | None:
    """Return a non-empty string value (also of a value object), or None."""
    value = literal(value)
    return value.strip() if isinstance(value, str) and value.strip() else None


def term(name: str, prefixes: Mapping[str, str] = PREFIXES) -> str:
    """Return the RO-Crate term a property name or `@type` stands for, or the name itself if it is no IRI of one.

    `schema:name`, `http://schema.org/name` and `https://schema.org/name` are `name`; the full IRI of a term
    in `TERM_IRIS` is that term. A keyword (`@id`) and a plain term are returned as they are.
    """
    if name.startswith("@"):
        return name
    iri = expand(name, prefixes)
    if iri.startswith(("http://schema.org/", "https://schema.org/")):
        local = iri.split("schema.org/", 1)[1]
        return local or name
    return _TERM_OF_IRI.get(iri, name)


def expand(name: str, prefixes: Mapping[str, str] = PREFIXES) -> str:
    """Return a compact IRI (`schema:name`) expanded with `prefixes`; anything else as it is."""
    prefix, sep, rest = name.partition(":")
    if sep and not rest.startswith("//") and prefix in prefixes:
        return prefixes[prefix] + rest
    return name


def normalize(value: Any) -> Any:
    """Return an entity (or a value) with its property names and `@type`s written as the RO-Crate terms.

    Nested objects are normalized too. When a crate gives the same property under two names (`name` and
    `schema:name`), the one already written as the term wins.
    """
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized: dict[str, Any] = {}
    # the names already written as terms first, so that they win over an IRI of the same term
    for name, item in sorted(value.items(), key=lambda pair: term(pair[0]) != pair[0]):
        key = term(name)
        if key in normalized:
            continue
        if key == "@type":
            normalized[key] = [_type_term(t) for t in item] if isinstance(item, list) else _type_term(item)
        elif key in ("@id", "@value", "@language"):
            normalized[key] = item
        else:
            normalized[key] = normalize(item)
    return normalized


def _type_term(value: Any) -> Any:
    """Return one `@type` value as the RO-Crate term (`schema:Dataset` is `Dataset`)."""
    return term(value) if isinstance(value, str) else value


def check_context(context: Any) -> None:
    """Refuse a crate whose `@context` gives a term the import reads another meaning than RO-Crate's.

    Only inline term definitions are inspected: a context URL is not fetched (the RO-Crate context, or any
    other - a crate without one is read as plain JSON too). A definition that adds a term, or that maps a
    term the import reads to the IRI the RO-Crate context gives it, is fine; one that maps it elsewhere - or
    removes it (`null`) - would make the import read a different property than the crate means.

    Raises:
        ValidationError: naming the redefined term.

    """
    inline = [definitions for definitions in as_list(context) if isinstance(definitions, dict)]
    # the prefixes the crate defines itself (`"ex": "https://example.org/terms#"`), for its compact IRIs
    prefixes = {
        **PREFIXES,
        **{
            name: definition
            for definitions in inline
            for name, definition in definitions.items()
            if not name.startswith("@") and isinstance(definition, str) and definition.endswith(("/", "#"))
        },
    }
    for definitions in inline:
        for name, definition in definitions.items():
            expected = _expected_iri(name)
            if expected is None or (isinstance(definition, dict) and "@id" not in definition):
                continue  # not a term the import reads, or a definition that only adds a type coercion
            iri = definition["@id"] if isinstance(definition, dict) else definition
            if isinstance(iri, str) and _same_iri(expand(iri, prefixes), expected):
                continue
            raise ValidationError(
                f"The crate's @context defines {name!r} as {iri!r}, but RO-Crate means {expected!r} by it; "
                "the import reads the RO-Crate meaning, so the crate is refused rather than misread"
            )


def _expected_iri(name: str) -> str | None:
    """Return the IRI the import assumes for a term it reads, or None for any other term."""
    if name in SCHEMA_TERMS:
        return SCHEMA_ORG + name
    return TERM_IRIS.get(name)


def _same_iri(iri: str, expected: str) -> bool:
    """Return whether two IRIs are the same, taking `https://schema.org/` for `http://schema.org/`."""
    return iri.replace("https://schema.org/", SCHEMA_ORG) == expected
