# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""The record's files as RO-Crate data entities, and the checksum vocabulary of the formats.

Shared by the formats so that they do not import each other: the RO-Crate exports describe the files in
the root's `hasPart` ([file_entities][oarepo_formats.files.file_entities], also the source of the README's
file table), and the RO-Crate import reads the checksums back
([CHECKSUM_TERMS][oarepo_formats.files.CHECKSUM_TERMS]).

RO-Crate itself has no checksum property (it leaves fixity to BagIt). The checksum Invenio stores for
every file is given with the terms of the
[Workflow Run RO-Crate vocabulary](https://www.researchobject.org/ro-terms/workflow-run) - `md5`,
`sha1`, `sha256`, `sha512`, hexadecimal strings on `File` entities - which
[add_checksum_terms][oarepo_formats.files.add_checksum_terms] defines in the crate's `@context`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

CHECKSUM_TERMS = {
    algorithm: f"https://w3id.org/ro/terms/workflow-run#{algorithm}"
    for algorithm in ("md5", "sha1", "sha256", "sha512")
}
"""Checksum term -> IRI: the checksum properties of `File` entities (Workflow Run RO-Crate terms)."""


def checksum_properties(checksum: str | None) -> dict[str, str]:
    """Return the checksum property of an Invenio checksum (`"md5:<hex>"` -> `{"md5": "<hex>"}`).

    Checksums of other algorithms than those in `CHECKSUM_TERMS` are left out.
    """
    algorithm, _, value = (checksum or "").partition(":")
    return {algorithm: value} if algorithm in CHECKSUM_TERMS and value else {}


def add_checksum_terms(document: dict) -> dict:
    """Define the checksum terms the document's entities use in its `@context`, in place.

    The context stays the plain RO-Crate context if no entity has a checksum; otherwise it becomes
    `[<RO-Crate context>, {"md5": "https://w3id.org/ro/terms/workflow-run#md5", ...}]`.
    """
    used = {term: iri for term, iri in CHECKSUM_TERMS.items() if any(term in e for e in document["@graph"])}
    if not used:
        return document
    context = document["@context"]
    if isinstance(context, str):
        context = [context, {}]
    context[1].update(used)
    document["@context"] = context
    return document


def _files_url(record: dict) -> str | None:
    """Return the URL of the record's files resource.

    RDM records carry `links.files`; models without that link template get the Invenio default
    route `<record self link>/files`.
    """
    links = record.get("links") or {}
    if links.get("files"):
        return links["files"]
    return f"{links['self']}/files" if links.get("self") else None


def file_entities(record: dict, include_hidden: bool = False) -> list[dict]:
    """Return the record's files as web-based ROCrate File data entities (spec 7.4.3).

    The `@id` is the direct download URL. The serialized record is already permission-filtered, so a
    user without access to files gets no data entities. Files whose upload is not completed are always
    skipped; hidden files only with `include_hidden` (the RO-Crate AIP export lists them, a download
    does not).

    Args:
        record: the serialized record (API JSON) with `links` and `files.entries`.
        include_hidden: whether to describe the files flagged `access.hidden`.

    Returns:
        `File` entities with `@id` (`<files url>/<quoted key>/content`), `name` and `localPath` (the
        file key), `contentSize`, `encodingFormat`, the stored checksum (e.g. `md5`, see
        `checksum_properties`) and `sdDatePublished` (the time of the export).

    """
    files_url = _files_url(record)
    entries = (record.get("files") or {}).get("entries") or {}
    if not files_url:
        return []
    accessed = datetime.now(UTC).isoformat(timespec="seconds")
    entities = []
    for key, entry in entries.items():
        if entry.get("status", "completed") != "completed":
            continue
        if (entry.get("access") or {}).get("hidden") and not include_hidden:
            continue
        entity: dict[str, Any] = {
            "@id": f"{files_url}/{quote(key)}/content",
            "@type": "File",
            "name": key,
            "localPath": key,
            "sdDatePublished": accessed,
        }
        if entry.get("size") is not None:
            entity["contentSize"] = str(entry["size"])
        if entry.get("mimetype"):
            entity["encodingFormat"] = entry["mimetype"]
        entity.update(checksum_properties(entry.get("checksum")))
        entities.append(entity)
    return entities
