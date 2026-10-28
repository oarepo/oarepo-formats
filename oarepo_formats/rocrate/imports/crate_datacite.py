# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Converting an RO-Crate's root data entity to the DataCite JSON the import hands to a model.

The import does not map a crate into a model's metadata itself: it converts the crate to **DataCite
JSON** and gives that to a DataCite import the deployment writes. This module is the
model-independent half of that - the inverse of
[DataCiteToROCrateSchema][oarepo_formats.rocrate.exports.schema.DataCiteToROCrateSchema] - and produces the same
flat attributes object the model's DataCite export writes (`titles`, `creators`, `publisher`,
`publicationYear`, ..., no `data.attributes` wrapper), so a deployment writes its DataCite import as the
inverse of its DataCite export.

A round trip can not restore everything: the export drops some DataCite properties on the way out (`lang`,
`schemeUri`, the title and description types, date types other than Issued, Created and Updated, ...),
and a crate can not carry what was dropped. The conversion is therefore
lossy in both directions, deliberately: it produces what the crate says, and leaves out what it does not.

The schema is subclassable, as the export's is, for a deployment whose models need something else.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from marshmallow import Schema, fields, post_dump

from ...datacite import DOI_REGISTRY, DOI_RESOLVER, pid_url
from ..exports.schema import WKT_CRS
from .jsonld import as_list, text, types

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

PID_URL_SCHEMES = {
    "https://orcid.org/": "ORCID",
    "https://ror.org/": "ROR",
    DOI_RESOLVER: "DOI",
}
"""URL prefix of a PID -> the scheme name DataCite uses for it (a Crossref Funder ID is a DOI here)."""

CROSSREF_FUNDER_PREFIX = "10.13039/"
"""A Crossref Funder ID is a DOI under this prefix (DataCite: `funderIdentifierType` Crossref Funder ID)."""

POINT = re.compile(r"^POINT\s*\(\s*(?P<longitude>[-+0-9.eE]+)\s+(?P<latitude>[-+0-9.eE]+)\s*\)$")
POLYGON = re.compile(r"^POLYGON\s*\(\s*\((?P<points>[^()]*)\)\s*\)$")
BARE_DOI = re.compile(r"^10\.\d{4,9}/\S+$")

RELATED_IDENTIFIER_TYPES = ("DOI", "arXiv", "Handle", "PMID", "URN", "bibcode", "SWHID", "RRID")
"""DataCite `relatedIdentifierType`s `pid_url` makes URLs for; DOI first, as a DOI is also a handle."""
"""A DOI without its resolver; the export writes a related work's `identifier` that way."""

RELATED = [
    ("isPartOf", "IsPartOf"),
    ("citation", "Cites"),
    ("isBasedOn", "IsDerivedFrom"),
    ("sameAs", "IsIdenticalTo"),
    ("subjectOf", "IsDescribedBy"),
]
"""schema.org property -> DataCite relation type, in the order the export maps them."""


DATE_TYPES = (("datePublished", "Issued"), ("dateCreated", "Created"), ("dateModified", "Updated"))
"""schema.org date property -> DataCite date type."""


class ROCrateToDataCiteSchema(Schema):
    """Convert a dereferenced root data entity to the flat DataCite attributes object.

    Every property is optional: a crate that does not carry it leaves it out, and the DataCite import (or
    the model's schema) decides whether the record is complete.
    """

    doi = fields.Method("get_doi")
    identifiers = fields.Method("get_identifiers")
    types = fields.Method("get_types")
    titles = fields.Method("get_titles")
    descriptions = fields.Method("get_descriptions")
    creators = fields.Method("get_creators")
    contributors = fields.Method("get_contributors")
    publisher = fields.Method("get_publisher")
    publicationYear = fields.Method("get_publication_year")  # noqa: N815 DataCite property name
    dates = fields.Method("get_dates")
    subjects = fields.Method("get_subjects")
    rightsList = fields.Method("get_rights_list")  # noqa: N815 DataCite property name
    language = fields.Method("get_language")
    version = fields.Method("get_version")
    relatedIdentifiers = fields.Method("get_related_identifiers")  # noqa: N815 DataCite property name
    fundingReferences = fields.Method("get_funding_references")  # noqa: N815 DataCite property name
    geoLocations = fields.Method("get_geo_locations")  # noqa: N815 DataCite property name

    @post_dump
    def drop_empty(self, data: dict, **kwargs: Any) -> dict:  # noqa: ARG002 marshmallow passes kwargs
        """Leave out the properties the crate does not carry."""
        return {key: value for key, value in data.items() if value not in (None, "", [], {})}

    #
    # identifiers
    #
    def get_doi(self, root: dict) -> str | None:
        """Return the DOI: from an `identifier` `PropertyValue`, the root `@id`, or `cite-as`."""
        for identifier in _property_values(root.get("identifier")):
            if identifier.get("propertyID") == DOI_REGISTRY:
                value = str(identifier.get("value") or "")
                return value.removeprefix("doi:") or None
        for candidate in (root.get("@id"), root.get("cite-as")):
            if isinstance(candidate, str) and candidate.startswith(DOI_RESOLVER):
                return candidate.removeprefix(DOI_RESOLVER) or None
        return None

    def get_identifiers(self, root: dict) -> list[dict] | None:
        """Return the identifiers that are not the DOI (`identifiers`, not `alternateIdentifiers`)."""
        identifiers = [
            {"identifierType": identifier["propertyID"], "identifier": str(identifier["value"])}
            for identifier in _property_values(root.get("identifier"))
            if identifier.get("propertyID") and identifier.get("propertyID") != DOI_REGISTRY and identifier.get("value")
        ]
        return identifiers or None

    def get_types(self, root: dict) -> dict:
        """Return the schema.org type of the resource as `types.schemaOrg`, and `Dataset` as the general type.

        DataCite requires `resourceTypeGeneral`; every crate this package reads is a `Dataset`, and a crate
        that names a more specific schema.org type keeps it in `schemaOrg`.
        """
        other = next((t for t in types(root) if t != "Dataset"), None)
        return {"resourceTypeGeneral": "Dataset", **({"schemaOrg": other} if other else {})}

    #
    # titles, descriptions
    #
    def get_titles(self, root: dict) -> list[dict] | None:
        """Return the `name` as the main title and `alternateName` as alternative titles."""
        main = [{"title": name}] if (name := text(root.get("name"))) else []
        alternates = [
            {"title": alternate_title, "titleType": "AlternativeTitle"}
            for alternate in as_list(root.get("alternateName"))
            if (alternate_title := text(alternate))
        ]
        return main + alternates or None

    def get_descriptions(self, root: dict) -> list[dict] | None:
        """Return the `description` as the abstract."""
        description = text(root.get("description"))
        return [{"description": description, "descriptionType": "Abstract"}] if description else None

    #
    # agents
    #
    def get_creators(self, root: dict) -> list[dict] | None:
        """Return `author` as DataCite creators."""
        return _agents(root.get("author")) or None

    def get_contributors(self, root: dict) -> list[dict] | None:
        """Return `contributor` as DataCite contributors (`contributorType` was never exported)."""
        return _agents(root.get("contributor")) or None

    def get_publisher(self, root: dict) -> dict | None:
        """Return the `publisher` Organization as a DataCite 4.5 publisher object."""
        publisher = root.get("publisher")
        if isinstance(publisher, str):
            publisher = {"name": publisher}
        if not isinstance(publisher, dict) or not text(publisher.get("name")):
            return None
        result: dict[str, Any] = {"name": text(publisher.get("name"))}
        if scheme_identifier := _scheme_identifier(publisher.get("@id")):
            result["publisherIdentifier"], result["publisherIdentifierScheme"] = scheme_identifier
        return result

    #
    # dates
    #
    def get_publication_year(self, root: dict) -> int | None:
        """Return the year of `datePublished` (spec: `publicationYear` is the year)."""
        published = text(root.get("datePublished"))
        if published and (match := re.match(r"^\s*(\d{4})", published)):
            return int(match.group(1))
        return None

    def get_dates(self, root: dict) -> list[dict] | None:
        """Return `datePublished`, `dateCreated` and `dateModified` as Issued, Created and Updated."""
        dates = [
            {"date": date, "dateType": date_type}
            for property_, date_type in DATE_TYPES
            if (date := text(root.get(property_)))
        ]
        return dates or None

    #
    # rights, subjects
    #
    def get_subjects(self, root: dict) -> list[dict] | None:
        """Return `keywords` and the `about` DefinedTerms as DataCite subjects."""
        subjects: dict[str, dict] = {}
        for keyword in as_list(root.get("keywords")):
            if keyword_text := text(keyword):
                subjects.setdefault(keyword_text, {"subject": keyword_text})
        for reference in as_list(root.get("about")):
            if not isinstance(reference, dict) or not (name := text(reference.get("name"))):
                continue
            subject = {"subject": name}
            if identifier := text(reference.get("@id")):
                subject["valueUri"] = identifier
            if scheme := text(reference.get("inDefinedTermSet")):
                subject["schemeUri"] = scheme
            subjects[name] = subject  # the term replaces the bare keyword of the same name
        return list(subjects.values()) or None

    def get_rights_list(self, root: dict) -> list[dict] | None:
        """Return `license` as DataCite rights."""
        rights = []
        for reference in as_list(root.get("license")):
            license_: dict[str, Any] = {}
            if isinstance(reference, str):
                license_["rights"] = reference
                license_["rightsUri"] = reference if reference.startswith(("http://", "https://")) else None
            elif isinstance(reference, dict):
                license_["rights"] = text(reference.get("name")) or text(reference.get("@id"))
                license_["rightsUri"] = text(reference.get("@id"))
                license_["rightsIdentifier"] = text(reference.get("identifier"))
            if license_ := {key: value for key, value in license_.items() if value}:
                rights.append(license_)
        return rights or None

    #
    # language, version
    #
    def get_language(self, root: dict) -> str | None:
        """Return `inLanguage`."""
        return text(root.get("inLanguage"))

    def get_version(self, root: dict) -> str | None:
        """Return `version`."""
        return text(root.get("version"))

    #
    # related works, funding, places
    #
    def get_related_identifiers(self, root: dict) -> list[dict] | None:
        """Return the related works as `relatedIdentifiers`, one relation type per schema.org property."""
        related = []
        for property_, relation_type in RELATED:
            for reference in as_list(root.get(property_)):
                if not isinstance(reference, dict):
                    continue
                identifier = text(reference.get("identifier")) or text(reference.get("@id"))
                if not identifier:
                    continue
                identifier_type = _identifier_type(identifier, text(reference.get("@id")))
                if identifier_type == "DOI":
                    # DataCite wants the bare DOI, not its resolver URL
                    identifier = identifier.removeprefix(DOI_RESOLVER)
                related.append(
                    {
                        "relatedIdentifier": identifier,
                        "relatedIdentifierType": identifier_type,
                        "relationType": relation_type,
                        "resourceTypeGeneral": "Dataset" if "Dataset" in types(reference) else "Other",
                    }
                )
        return related or None

    def get_funding_references(self, root: dict) -> list[dict] | None:
        """Return the `funding` Grants and the standalone `funder`s as `fundingReferences`."""
        funding = []
        funders_in_grants: set[str] = set()
        for grant in as_list(root.get("funding")):
            if not isinstance(grant, dict):
                continue
            reference = _funder_fields(grant.get("funder"))
            funders_in_grants.add(_funder_key(grant.get("funder")))
            reference["awardNumber"] = text(grant.get("identifier"))
            reference["awardTitle"] = text(grant.get("name"))
            reference["awardUri"] = text(grant.get("@id")) or text(grant.get("url"))
            if cleaned := {key: value for key, value in reference.items() if value}:
                funding.append(cleaned)
        standalone = [
            _funder_fields(funder)
            for funder in as_list(root.get("funder"))
            if isinstance(funder, dict) and _funder_key(funder) not in funders_in_grants
        ]
        funding.extend({key: value for key, value in reference.items() if value} for reference in standalone)
        return [reference for reference in funding if reference] or None

    def get_geo_locations(self, root: dict) -> list[dict] | None:
        """Return `spatialCoverage` Places with their WKT geometries as `geoLocations`."""
        locations = []
        for place in as_list(root.get("spatialCoverage")):
            if not isinstance(place, dict):
                continue
            location: dict[str, Any] = {}
            if name := text(place.get("name")):
                location["geoLocationPlace"] = name
            for geometry in as_list(place.get("geo")):
                wkt = _wkt(geometry)
                if point := POINT.match(wkt) if wkt else None:
                    location["geoLocationPoint"] = {
                        "pointLongitude": float(point.group("longitude")),
                        "pointLatitude": float(point.group("latitude")),
                    }
                elif polygon := POLYGON.match(wkt) if wkt else None:
                    location["geoLocationPolygon"] = [
                        {"polygonPoint": {"pointLongitude": float(longitude), "pointLatitude": float(latitude)}}
                        for longitude, latitude in _points(polygon.group("points"))
                    ]
            if location:
                locations.append(location)
        return locations or None


def _funder_fields(funder: Any) -> dict[str, Any]:
    """Return the DataCite funder fields of an Organization entity."""
    if not isinstance(funder, dict):
        return {}
    fields: dict[str, Any] = {"funderName": text(funder.get("name"))}
    if scheme_identifier := _funder_identifier(funder.get("@id")):
        fields["funderIdentifier"], fields["funderIdentifierType"] = scheme_identifier
    return fields


def _funder_identifier(url: Any) -> tuple[str, str] | None:
    """Return `(identifier, "Crossref Funder ID")` for a funder DOI URL, else the plain scheme lookup.

    A Crossref Funder ID *is* a DOI (`10.13039/...`), so `PID_URL_SCHEMES` alone would answer `DOI` -
    which is not a valid `funderIdentifierType`. DataCite accepts Crossref Funder ID, ROR, GRID, ISNI and
    Other.
    """
    if isinstance(url, str) and url.startswith(DOI_RESOLVER):
        identifier = url.removeprefix(DOI_RESOLVER)
        if identifier.startswith(CROSSREF_FUNDER_PREFIX):
            return identifier, "Crossref Funder ID"
    return _scheme_identifier(url)


def _funder_key(funder: Any) -> str:
    """Return what identifies a funder, to avoid listing the funder of a grant twice."""
    if not isinstance(funder, dict):
        return ""
    return text(funder.get("@id")) or text(funder.get("name")) or ""


def _agents(value: Any) -> list[dict]:
    """Return schema.org Person/Organization entities as DataCite creators or contributors."""
    agents = [_agent(entity) for entity in as_list(value) if isinstance(entity, dict)]
    return [agent for agent in agents if agent]


def _agent(entity: Mapping[str, Any]) -> dict | None:
    """Return one Person/Organization entity as a DataCite creator or contributor, or None without a name."""
    if not (name := text(entity.get("name"))):
        return None
    agent: dict[str, Any] = {"name": name}
    organizational = "Organization" in types(entity)
    agent["nameType"] = "Organizational" if organizational else "Personal"
    if not organizational:
        if given := text(entity.get("givenName")):
            agent["givenName"] = given
        if family := text(entity.get("familyName")):
            agent["familyName"] = family
    if scheme_identifier := _scheme_identifier(entity.get("@id")):
        identifier, scheme = scheme_identifier
        agent["nameIdentifiers"] = [{"nameIdentifier": identifier, "nameIdentifierScheme": scheme}]
    affiliations = [
        affiliation
        for reference in as_list(entity.get("affiliation"))
        if isinstance(reference, dict) and (affiliation := _affiliation(reference))
    ]
    if affiliations:
        agent["affiliation"] = affiliations
    return agent


def _affiliation(entity: Mapping[str, Any]) -> dict:
    """Return one affiliation Organization as a DataCite affiliation."""
    affiliation: dict[str, Any] = {"name": text(entity.get("name"))}
    if scheme_identifier := _scheme_identifier(entity.get("@id")):
        affiliation["affiliationIdentifier"], affiliation["affiliationIdentifierScheme"] = scheme_identifier
    return {key: value for key, value in affiliation.items() if value}


def _scheme_identifier(url: Any) -> tuple[str, str] | None:
    """Return `(identifier, DataCite scheme)` for a URL of a known PID, e.g. an ORCID URL."""
    if not isinstance(url, str):
        return None
    for prefix, scheme in PID_URL_SCHEMES.items():
        if url.startswith(prefix) and len(url) > len(prefix):
            return url.removeprefix(prefix), scheme
    return None


def _identifier_type(identifier: str, url: str | None) -> str:
    """Return the DataCite `relatedIdentifierType` of an identifier, `Other` if it is not known.

    A bare identifier gets the type the export would have made its URL (the entity's `@id`) from, so a
    value that merely looks like a PMID or a handle is not taken for one.
    """
    if identifier.startswith(DOI_RESOLVER) or BARE_DOI.match(identifier):
        return "DOI"
    if identifier.startswith(("http://", "https://")):
        return "URL"
    return next((t for t in RELATED_IDENTIFIER_TYPES if url and pid_url(identifier, t) == url), "Other")


def _property_values(value: Any) -> list[dict]:
    """Return the `PropertyValue` entities of an `identifier` property."""
    return [
        entity
        for entity in as_list(value)
        if isinstance(entity, dict) and isinstance(entity.get("propertyID"), str) and entity.get("value") is not None
    ]


def _wkt(geometry: Any) -> str | None:
    """Return the WKT of a `Geometry` entity without its coordinate reference system."""
    if not isinstance(geometry, dict):
        return None
    wkt = text(geometry.get("asWKT"))
    if not wkt:
        return None
    return wkt.removeprefix(WKT_CRS).strip() or None


def _points(points: str) -> Iterable[tuple[float, float]]:
    """Return the `(longitude, latitude)` pairs of a WKT polygon ring, without a repeated closing point."""
    coordinates = [tuple(part.split()) for part in points.split(",") if part.strip()]
    parsed = [(float(longitude), float(latitude)) for longitude, latitude in coordinates]
    if len(parsed) > 1 and parsed[0] == parsed[-1]:
        parsed = parsed[:-1]
    return parsed
