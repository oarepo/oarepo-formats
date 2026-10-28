# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Flattening of nested schema.org JSON-LD into an RO-Crate metadata document.

The RO-Crate metadata document is *flattened* JSON-LD (spec 4.2): a `@graph` list of entities that
reference each other by `{"@id": ...}`. [flatten][oarepo_formats.rocrate.exports.graph.flatten] produces it from the
nested tree dumped by [DataCiteToROCrateSchema][oarepo_formats.rocrate.exports.schema.DataCiteToROCrateSchema], so the
schema does not need to know about the graph.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

ROCRATE_VERSION = "1.3"
"""Targeted RO-Crate specification version (https://w3id.org/ro/crate/1.3).

The `@context` and the descriptor's `conformsTo` are derived from it.
"""


def to_json_bytes(document: Any, **options: Any) -> bytes:
    """Serialize a crate document (or a part of it) to UTF-8 JSON bytes.

    A lazy translatable string (`flask_babel.speaklater.LazyString`) is not JSON-serializable; it is
    written as its string value, so a value taken from the configuration - e.g. `THEME_SITENAME` in
    the provenance's `SoftwareApplication` - can go into the crate.
    """
    return json.dumps(document, ensure_ascii=False, default=str, **options).encode()


def local_id(entity: dict[str, Any]) -> str:
    """Return a deterministic local `#` identifier for an entity without one, derived from its content.

    Equal entities (e.g. the same person without an ORCID listed as author and contributor) get the same
    identifier and are therefore merged into one.

    Args:
        entity: the entity, without `@id`; its nested entities already replaced by references.

    Returns:
        `#<first @type in lowercase>-<12 hex digits of the content hash>`, e.g. `#person-0123456789ab`.

    """
    types = entity.get("@type", "entity")
    type_ = (types[0] if isinstance(types, list) else types).lower()
    digest = hashlib.sha1(json.dumps(entity, sort_keys=True, default=str).encode(), usedforsecurity=False).hexdigest()
    return f"#{type_}-{digest[:12]}"


def flatten(tree: dict[str, Any]) -> dict[str, Any]:
    """Flatten a nested JSON-LD tree describing the root data entity into an RO-Crate metadata document.

    - Every nested object becomes a separate entity in `@graph` and is replaced by a `{"@id": ...}`
      reference.
    - Objects without `@id` get a local one ([local_id][oarepo_formats.rocrate.exports.graph.local_id]).
    - Entities with the same `@id` are merged; the first value of a property wins.
    - Duplicate references in a list and empty properties of the root are dropped.
    - The root keeps its `@id`, or `./` if the tree has none.

    Args:
        tree: nested JSON-LD of the root data entity.

    Returns:
        The RO-Crate metadata document `{"@context": ..., "@graph": [...]}`; the descriptor is at
        `DESCRIPTOR_INDEX`, the root data entity at `ROOT_INDEX` (read them through
        [CrateContent][oarepo_formats.rocrate.exports.zip.CrateContent]).

    """
    root_id = tree.get("@id", "./")
    root: dict[str, Any] = {"@id": root_id}
    entities: dict[str, dict[str, Any]] = {root_id: root}

    def extract(value: Any) -> Any:
        if isinstance(value, list):
            references = [extract(v) for v in value]
            return [r for i, r in enumerate(references) if r not in references[:i]]  # drop duplicates
        if not isinstance(value, dict):
            return value
        entity = {k: extract(v) for k, v in value.items()}
        if set(entity) <= {"@id"}:  # already a reference
            return entity
        entity.setdefault("@id", local_id(entity))
        existing = entities.setdefault(entity["@id"], entity)
        for key, item in entity.items():
            existing.setdefault(key, item)
        return {"@id": entity["@id"]}

    for key, value in tree.items():
        if key != "@id" and value not in (None, "", [], {}):
            root[key] = extract(value)

    descriptor = {
        "@id": "ro-crate-metadata.json",
        "@type": "CreativeWork",
        "conformsTo": {"@id": f"https://w3id.org/ro/crate/{ROCRATE_VERSION}"},
        "about": {"@id": root_id},
    }
    return {
        "@context": f"https://w3id.org/ro/crate/{ROCRATE_VERSION}/context",
        "@graph": [descriptor, *entities.values()],  # see DESCRIPTOR_INDEX and ROOT_INDEX
    }


DESCRIPTOR_INDEX = 0
"""Position of the `ro-crate-metadata.json` descriptor in the `@graph` of a document made by `flatten`."""

ROOT_INDEX = 1
"""Position of the root data entity (the dataset the crate describes) in the `@graph` made by `flatten`."""
