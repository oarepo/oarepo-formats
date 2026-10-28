# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""The DataCite JSON contract the formats share, and the identifiers of a DataCite record.

The RO-Crate and README exports are both converted from the model's DataCite JSON export, so both need
its mimetype and the helpers that turn the identifiers DataCite carries into resolvable URLs. They live
here, at the package level, so that one format never imports another.
"""

from __future__ import annotations

from collections.abc import Sequence

from flask import current_app, has_app_context
from idutils.detectors import detect_identifier_schemes
from idutils.normalizers import to_url

from .config import OAREPO_FORMATS_PID_SCHEMES

DATACITE_JSON_MIMETYPE = "application/vnd.datacite.datacite+json"
"""Mimetype of the model's DataCite JSON export the formats are converted from."""

DOI_RESOLVER = "https://doi.org/"
"""URL prefix turning a bare DOI into its resolvable URL."""

DOI_REGISTRY = "https://registry.identifiers.org/registry/doi"
"""`propertyID` of DOI identifiers."""

SCHEME_ALIASES = {"crossref funder id": "doi", "bibcode": "ads", "swhid": "swh"}
"""DataCite identifier scheme (lowercase) -> the idutils scheme of the same identifier."""


def pid_url(value: str | None, scheme: str | None = None) -> str | None:
    """Return a resolvable URL for an identifier, or None if it can not be made into one.

    URLs are returned as they are; bare identifiers are turned into URLs by `idutils.to_url`
    (`pid_url("0000-0002-1825-0097", "ORCID")` is `https://orcid.org/0000-0002-1825-0097`). Identifiers
    that are not valid for their scheme, and identifiers of schemes idutils has no landing page for (local
    accession numbers, ISBNs, ISNIs, ...) have no URL.
    """
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    scheme = (scheme or "").lower()
    scheme = SCHEME_ALIASES.get(scheme, scheme)
    # idutils normalizers expect a valid identifier - they mangle or fail on anything else
    if not scheme or scheme not in detect_identifier_schemes(value):
        return None
    url = to_url(value, scheme, url_scheme="https")
    return url if url.startswith(("http://", "https://")) else None


def doi(datacite: dict) -> str | None:
    """Return the bare DOI of a DataCite record, if any."""
    value = datacite.get("doi") or next(
        (
            i.get("identifier")
            for i in datacite.get("identifiers") or []
            if (i.get("identifierType") or "").upper() == "DOI"
        ),
        None,
    )
    return value.removeprefix(DOI_RESOLVER) if value else None


def pid_schemes(record: dict, datacite: dict) -> Sequence[str]:
    """Return the DataCite identifier types of the record's PID in priority order (`OAREPO_FORMATS_PID_SCHEMES`)."""
    schemes = current_app.config["OAREPO_FORMATS_PID_SCHEMES"] if has_app_context() else OAREPO_FORMATS_PID_SCHEMES
    return schemes if isinstance(schemes, Sequence) else schemes(record, datacite)


def primary_pid(record: dict, datacite: dict) -> tuple[str, str] | None:
    """Return `(identifier, identifierType)` of the record's PID, or None if it has none.

    The PID is the record's identifier of the first type in [pid_schemes][oarepo_formats.datacite.pid_schemes]
    the record has - its DOI by default. Turn it into a URL with `pid_url(*pid)`.
    """
    by_type: dict[str, tuple[str, str]] = {}
    for i in datacite.get("identifiers") or []:
        if (value := i.get("identifier")) and (scheme := i.get("identifierType")):
            by_type.setdefault(scheme.upper(), (value, scheme))
    if value := doi(datacite):
        by_type["DOI"] = (value, "DOI")
    for scheme in pid_schemes(record, datacite):
        if found := by_type.get(scheme.upper()):
            return found
    return None
