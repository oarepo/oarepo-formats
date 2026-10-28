# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Navigating an RO-Crate metadata document in tests, as the spec describes - not by position."""

from __future__ import annotations

METADATA_DESCRIPTOR = "ro-crate-metadata.json"


def descriptor_of(document: dict) -> dict:
    """Return the `ro-crate-metadata.json` descriptor of a metadata document."""
    return next(entity for entity in document["@graph"] if entity["@id"] == METADATA_DESCRIPTOR)


def root_of(document: dict) -> dict:
    """Return the root data entity of a metadata document: the entity its descriptor is `about`."""
    root_id = descriptor_of(document)["about"]["@id"]
    return next(entity for entity in document["@graph"] if entity["@id"] == root_id)
