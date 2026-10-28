# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Marshmallow schema converting DataCite JSON (kernel 4.3/4.5) to nested schema.org JSON-LD.

[DataCiteToROCrateSchema][oarepo_formats.rocrate.exports.schema.DataCiteToROCrateSchema] describes the root data
entity of the RO-Crate. Subclass it to change the mapping (see [oarepo_formats.rocrate][] for an example).

## Input

The object passed to `dump`:

```python
{
    "datacite": {
        ...
    },  # DataCite attributes from the model's DataCite JSON export
    "record": {
        ...
    },  # the serialized record (API JSON): links, files, ...
}
```

## Output

Nested schema.org JSON-LD of the root data entity, for example:

```python
{
    "@id": "https://doi.org/10.1234/abcd",
    "@type": "Dataset",
    "name": "Title",
    "author": [
        {
            "@id": "https://orcid.org/0000-0001-...",
            "@type": "Person",
            "name": "Doe, Jane",
        }
    ],
}
```

[flatten][oarepo_formats.rocrate.exports.graph.flatten] turns it into the RO-Crate metadata document.

## Conventions for fields (including the ones in subclasses)

- The field name (or `data_key` for `@id`, `@type` and `cite-as`) is the schema.org property.
- Return `missing` ([or_missing][oarepo_formats.rocrate.exports.schema.or_missing] helps) to leave a property out.
- Return nested objects with `@type` for related entities. Give them `@id` only when there is a
  resolvable identifier ([pid_url][oarepo_formats.datacite.pid_url] and
  [organization][oarepo_formats.rocrate.exports.schema.organization] help); flattening gives the others a local id.
"""

from __future__ import annotations

from typing import Any

from marshmallow import Schema, fields, missing

from ...datacite import DOI_REGISTRY, DOI_RESOLVER, doi, pid_url, primary_pid
from ...files import file_entities

WKT_CRS = "<http://www.opengis.net/def/crs/OGC/1.3/CRS84>"
"""Coordinate reference system prefixed to every WKT geometry (spec 8.11): longitude, latitude."""


def iso_date(value: Any) -> str | None:
    """Return a single ISO 8601 date; DataCite date ranges (`start/end`) are reduced to their start."""
    if value in (None, ""):
        return None
    return str(value).split("/", maxsplit=1)[0] or None


def organization(name: str | None, identifier: str | None = None, scheme: str | None = None) -> dict:
    """Return an Organization, identified by its PID when it has a resolvable one."""
    entity: dict[str, Any] = {"@type": "Organization"}
    if url := pid_url(identifier, scheme):
        entity["@id"] = url
    if name:
        entity["name"] = name
    return entity


def or_missing(value: Any) -> Any:
    """Return `missing` for empty values so that marshmallow leaves the property out."""
    return value if value not in (None, "", [], {}) else missing


class AgentSchema(Schema):
    """DataCite creator/contributor to schema.org Person or Organization.

    | Property | Source |
    |---|---|
    | `@id` | first `nameIdentifiers` entry that has a URL (ORCID, ROR, ...) |
    | `@type` | `Organization` for `nameType: Organizational`, else `Person` |
    | `name`, `givenName`, `familyName` | the same DataCite properties |
    | `affiliation` | `affiliation` (Organization, `@id` from `affiliationIdentifier`) |

    `contributorType` has no schema.org equivalent and is dropped.
    """

    id_ = fields.Method("get_id", data_key="@id")
    type_ = fields.Method("get_type", data_key="@type")
    name = fields.Str()
    givenName = fields.Str()  # noqa: N815 schema.org property name
    familyName = fields.Str()  # noqa: N815 schema.org property name
    affiliation = fields.Method("get_affiliation")

    def get_id(self, obj: dict) -> Any:
        """Return the first name identifier that can be expressed as a URL (ORCID, ROR, ...)."""
        return or_missing(
            next(
                (
                    url
                    for ni in obj.get("nameIdentifiers") or []
                    if (url := pid_url(ni.get("nameIdentifier"), ni.get("nameIdentifierScheme")))
                ),
                None,
            )
        )

    def get_type(self, obj: dict) -> str:
        """Return Organization for organizational names, Person otherwise."""
        return "Organization" if obj.get("nameType") == "Organizational" else "Person"

    def get_affiliation(self, obj: dict) -> Any:
        """Return the affiliations as Organizations."""
        return or_missing(
            [
                organization(a.get("name"), a.get("affiliationIdentifier"), a.get("affiliationIdentifierScheme"))
                for a in obj.get("affiliation") or []
            ]
        )


class DataCiteToROCrateSchema(Schema):
    """DataCite JSON to the nested schema.org JSON-LD description of the RO-Crate root data entity.

    | Property | Source |
    |---|---|
    | `@id` | PID URL (`OAREPO_FORMATS_PID_SCHEMES`, the DOI by default), else the landing page (`links.self_html`) |
    | `@type` | `Dataset`, plus `types.schemaOrg` if different |
    | `cite-as` | PID URL |
    | `identifier` | DOI, `identifiers`, `alternateIdentifiers` (PropertyValue) |
    | `name`, `alternateName` | `titles` without / with `titleType` |
    | `description` | the `Abstract` description, else the first one |
    | `author`, `contributor` | `creators`, `contributors` (Person / Organization) |
    | `publisher` | `publisher` (Organization) |
    | `datePublished` | `Issued` date, else `publicationYear` |
    | `dateCreated`, `dateModified` | `Created`, `Updated` dates |
    | `license` | `rightsList` (CreativeWork) |
    | `keywords`, `about` | `subjects`; `about` only those with `valueUri` (DefinedTerm) |
    | `inLanguage`, `version` | `language`, `version` |
    | `funder`, `funding` | `fundingReferences` (Organization, Grant) |
    | `spatialCoverage` | `geoLocations` (Place with WKT Geometry) |
    | `isPartOf` | `relatedIdentifiers` with `IsPartOf` |
    | `citation` | `relatedIdentifiers` with `Cites`, `References` |
    | `isBasedOn` | `relatedIdentifiers` with `IsDerivedFrom`, `IsNewVersionOf`, `IsVersionOf` |
    | `sameAs` | `relatedIdentifiers` with `IsIdenticalTo` |
    | `subjectOf` | `relatedIdentifiers` with `IsDescribedBy` |
    | `hasPart` | the record's files ([file_entities][oarepo_formats.files.file_entities]) |

    To change or add a property, subclass and override or add a field or its `get_*` method. The
    `get_*` methods receive the whole dumped object, `{"datacite": ..., "record": ...}`.
    """

    id_ = fields.Method("get_id", data_key="@id")
    type_ = fields.Method("get_type", data_key="@type")
    cite_as = fields.Method("get_cite_as", data_key="cite-as")
    identifier = fields.Method("get_identifier")
    name = fields.Method("get_name")
    alternateName = fields.Method("get_alternate_name")  # noqa: N815 schema.org property name
    description = fields.Method("get_description")
    author = fields.List(fields.Nested(AgentSchema), attribute="datacite.creators")
    contributor = fields.List(fields.Nested(AgentSchema), attribute="datacite.contributors")
    publisher = fields.Method("get_publisher")
    datePublished = fields.Method("get_date_published")  # noqa: N815 schema.org property name
    dateCreated = fields.Method("get_date_created")  # noqa: N815 schema.org property name
    dateModified = fields.Method("get_date_modified")  # noqa: N815 schema.org property name
    license = fields.Method("get_license")
    keywords = fields.Method("get_keywords")
    about = fields.Method("get_about")
    inLanguage = fields.Str(attribute="datacite.language")  # noqa: N815 schema.org property name
    version = fields.Str(attribute="datacite.version")
    funder = fields.Method("get_funder")
    funding = fields.Method("get_funding")
    spatialCoverage = fields.Method("get_spatial_coverage")  # noqa: N815 schema.org property name
    isPartOf = fields.Method("get_is_part_of")  # noqa: N815 schema.org property name
    citation = fields.Method("get_citation")
    isBasedOn = fields.Method("get_is_based_on")  # noqa: N815 schema.org property name
    sameAs = fields.Method("get_same_as")  # noqa: N815 schema.org property name
    subjectOf = fields.Method("get_subject_of")  # noqa: N815 schema.org property name
    hasPart = fields.Method("get_has_part")  # noqa: N815 schema.org property name

    include_hidden_files = False
    """Whether `hasPart` describes the files flagged `access.hidden`; the RO-Crate AIP export sets it."""

    # root

    def get_id(self, obj: dict) -> Any:
        """Return the PID URL, else the landing page (flattening uses `./` when neither is known)."""
        return or_missing(self._pid_url(obj) or (obj["record"].get("links") or {}).get("self_html"))

    def get_type(self, obj: dict) -> str | list[str]:
        """Return Dataset (spec 6.2), plus the schema.org type of the resource if it is a different one."""
        schema_org = (obj["datacite"].get("types") or {}).get("schemaOrg")
        return ["Dataset", schema_org] if schema_org and schema_org != "Dataset" else "Dataset"

    # identifiers

    def get_cite_as(self, obj: dict) -> Any:
        """Return the persistent identifier to cite the crate with (spec 6.2.1.1)."""
        return or_missing(self._pid_url(obj))

    def _pid_url(self, obj: dict) -> str | None:
        """Return the URL of the record's PID (`OAREPO_FORMATS_PID_SCHEMES`, the DOI by default)."""
        pid = primary_pid(obj["record"], obj["datacite"])
        return pid_url(*pid) if pid else None

    def get_identifier(self, obj: dict) -> Any:
        """Return the DOI, identifiers and alternate identifiers as PropertyValues (spec 6.2.1)."""
        datacite = obj["datacite"]
        identifiers = []
        if value := doi(datacite):
            identifiers.append(
                {
                    "@type": "PropertyValue",
                    "propertyID": DOI_REGISTRY,
                    "value": f"doi:{value}",
                    "url": f"{DOI_RESOLVER}{value}",
                }
            )
        others = [
            (i.get("identifierType"), i.get("identifier"))
            for i in datacite.get("identifiers") or []
            if (i.get("identifierType") or "").upper() != "DOI"
        ] + [
            (i.get("alternateIdentifierType"), i.get("alternateIdentifier"))
            for i in datacite.get("alternateIdentifiers") or []
        ]
        for scheme, value in dict.fromkeys(others):
            if scheme and value:
                identifier = {"@type": "PropertyValue", "propertyID": scheme, "value": value}
                if url := pid_url(value):
                    identifier["url"] = url
                identifiers.append(identifier)
        return or_missing(identifiers)

    # titles, descriptions

    def _titles(self, obj: dict) -> list[dict]:
        return [t for t in obj["datacite"].get("titles") or [] if t.get("title")]

    def _main_title(self, obj: dict) -> dict | None:
        titles = self._titles(obj)
        return next((t for t in titles if not t.get("titleType")), titles[0] if titles else None)

    def get_name(self, obj: dict) -> Any:
        """Return the main title (the one without `titleType`)."""
        main = self._main_title(obj)
        return main["title"] if main else missing

    def get_alternate_name(self, obj: dict) -> Any:
        """Return subtitles, alternative and translated titles."""
        main = self._main_title(obj)
        return or_missing([t["title"] for t in self._titles(obj) if t is not main])

    def get_description(self, obj: dict) -> Any:
        """Return the abstract, or the first description; other description types are dropped."""
        descriptions = [d for d in obj["datacite"].get("descriptions") or [] if d.get("description")]
        chosen = next((d for d in descriptions if d.get("descriptionType") == "Abstract"), None)
        chosen = chosen or (descriptions[0] if descriptions else None)
        return chosen["description"] if chosen else missing

    # agents

    def get_publisher(self, obj: dict) -> Any:
        """Return the publisher (a string in DataCite 4.3, an object in 4.5) as an Organization."""
        publisher = obj["datacite"].get("publisher")
        if not publisher:
            return missing
        if isinstance(publisher, str):
            publisher = {"name": publisher}
        return organization(
            publisher.get("name"),
            publisher.get("publisherIdentifier"),
            publisher.get("publisherIdentifierScheme"),
        )

    # dates

    def _dates(self, obj: dict) -> dict[str, str | None]:
        """Return the first date of each date type."""
        dates: dict[str, str | None] = {}
        for date in obj["datacite"].get("dates") or []:
            dates.setdefault(date.get("dateType"), iso_date(date.get("date")))
        return dates

    def get_date_published(self, obj: dict) -> Any:
        """Return the Issued date, else the publication year (MUST, spec 6.2)."""
        return or_missing(self._dates(obj).get("Issued") or iso_date(obj["datacite"].get("publicationYear")))

    def get_date_created(self, obj: dict) -> Any:
        """Return the Created date."""
        return or_missing(self._dates(obj).get("Created"))

    def get_date_modified(self, obj: dict) -> Any:
        """Return the Updated date."""
        return or_missing(self._dates(obj).get("Updated"))

    # rights, subjects

    def get_license(self, obj: dict) -> Any:
        """Return the rights as CreativeWorks identified by their URI (spec 8.9)."""
        licenses = []
        for rights in obj["datacite"].get("rightsList") or []:
            name = rights.get("rights")
            uri = rights.get("rightsUri") or pid_url(name)
            if not (name or uri):
                continue
            license_: dict[str, Any] = {"@type": "CreativeWork"}
            if uri:
                license_["@id"] = uri
            if name:
                license_["name"] = name
            if rights.get("rightsIdentifier"):
                license_["identifier"] = rights["rightsIdentifier"]
            licenses.append(license_)
        return or_missing(licenses)

    def get_keywords(self, obj: dict) -> Any:
        """Return all subjects as keywords."""
        subjects = obj["datacite"].get("subjects") or []
        return or_missing(list(dict.fromkeys(s["subject"] for s in subjects if s.get("subject"))))

    def get_about(self, obj: dict) -> Any:
        """Return subjects from a vocabulary (with `valueUri`) as DefinedTerms."""
        terms = []
        for subject in obj["datacite"].get("subjects") or []:
            if subject.get("subject") and subject.get("valueUri"):
                term = {"@id": subject["valueUri"], "@type": "DefinedTerm", "name": subject["subject"]}
                if subject.get("schemeUri"):
                    term["inDefinedTermSet"] = subject["schemeUri"]
                terms.append(term)
        return or_missing(terms)

    # funding

    def _funder(self, funding: dict) -> dict | None:
        if not (funding.get("funderName") or funding.get("funderIdentifier")):
            return None
        return organization(
            funding.get("funderName"),
            funding.get("funderIdentifier"),
            funding.get("funderIdentifierType"),
        )

    def get_funder(self, obj: dict) -> Any:
        """Return the funders as Organizations, referenced directly from the root (spec 8.8)."""
        return or_missing([f for f in map(self._funder, obj["datacite"].get("fundingReferences") or []) if f])

    def get_funding(self, obj: dict) -> Any:
        """Return the awards as Grants with their funder."""
        grants = []
        for funding in obj["datacite"].get("fundingReferences") or []:
            number, title, uri = funding.get("awardNumber"), funding.get("awardTitle"), funding.get("awardUri")
            if not (number or title or uri):
                continue
            grant: dict[str, Any] = {"@type": "Grant"}
            if uri:
                grant.update({"@id": uri, "url": uri})
            if title:
                grant["name"] = title
            if number:
                grant["identifier"] = number
            if funder := self._funder(funding):
                grant["funder"] = funder
            grants.append(grant)
        return or_missing(grants)

    # places

    def get_spatial_coverage(self, obj: dict) -> Any:
        """Return the geo locations as Places with WKT Geometries (spec 8.11)."""
        places = []
        for location in obj["datacite"].get("geoLocations") or []:
            place: dict[str, Any] = {"@type": "Place"}
            if location.get("geoLocationPlace"):
                place["name"] = location["geoLocationPlace"]
            if wkts := self._wkts(location):
                place["geo"] = [{"@type": "Geometry", "asWKT": f"{WKT_CRS} {wkt}"} for wkt in wkts]
            if len(place) > 1:
                places.append(place)
        return or_missing(places)

    def _wkts(self, location: dict) -> list[str]:
        """Return WKT of the point, box and polygon of a DataCite geo location."""
        wkts = []
        if point := location.get("geoLocationPoint"):
            wkts.append(f"POINT ({point['pointLongitude']} {point['pointLatitude']})")
        if box := location.get("geoLocationBox"):
            w, e = box["westBoundLongitude"], box["eastBoundLongitude"]
            s, n = box["southBoundLatitude"], box["northBoundLatitude"]
            wkts.append(f"POLYGON (({w} {s}, {e} {s}, {e} {n}, {w} {n}, {w} {s}))")
        if polygon := location.get("geoLocationPolygon"):
            points = [p["polygonPoint"] for p in polygon]
            ring = ", ".join(f"{p['pointLongitude']} {p['pointLatitude']}" for p in points)
            wkts.append(f"POLYGON (({ring}))")
        return wkts

    # related works; DataCite HasPart is deliberately not mapped, hasPart is reserved for data entities

    def _related(self, obj: dict, *relation_types: str) -> Any:
        """Return related identifiers of the given relation types that can be expressed as a URL."""
        works = []
        for related in obj["datacite"].get("relatedIdentifiers") or []:
            url = pid_url(related.get("relatedIdentifier"), related.get("relatedIdentifierType"))
            if related.get("relationType") in relation_types and url:
                works.append(
                    {
                        "@id": url,
                        "@type": "Dataset" if related.get("resourceTypeGeneral") == "Dataset" else "CreativeWork",
                        "identifier": related["relatedIdentifier"],
                    }
                )
        return or_missing(works)

    def get_is_part_of(self, obj: dict) -> Any:
        """Return IsPartOf related works."""
        return self._related(obj, "IsPartOf")

    def get_citation(self, obj: dict) -> Any:
        """Return Cites and References related works."""
        return self._related(obj, "Cites", "References")

    def get_is_based_on(self, obj: dict) -> Any:
        """Return IsDerivedFrom, IsNewVersionOf and IsVersionOf related works."""
        return self._related(obj, "IsDerivedFrom", "IsNewVersionOf", "IsVersionOf")

    def get_same_as(self, obj: dict) -> Any:
        """Return IsIdenticalTo related works."""
        return self._related(obj, "IsIdenticalTo")

    def get_subject_of(self, obj: dict) -> Any:
        """Return IsDescribedBy related works."""
        return self._related(obj, "IsDescribedBy")

    # data entities

    def get_has_part(self, obj: dict) -> Any:
        """Return the files of the record (not in DataCite) as web-based File data entities."""
        return or_missing(file_entities(obj["record"], include_hidden=self.include_hidden_files))
