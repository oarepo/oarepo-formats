# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Default configuration shared by the formats; values in `invenio.cfg` win."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

OAREPO_FORMATS_PID_SCHEMES: Sequence[str] | Callable[[dict, dict], Sequence[str]] = ["DOI"]
"""DataCite `identifierType`s of the record's own PID, in priority order.

The first one the record has is its PID - the README's `pid`, the RO-Crate's `@id` and `cite-as` (see
[primary_pid][oarepo_formats.datacite.primary_pid]). Either a list, e.g. `["DOI", "Handle", "ARK"]` for a
repository minting ePIC handles or ARKs, or a callable `(record, datacite) -> list` choosing the list per
record (e.g. per model). The record is the serialized record (API JSON), datacite its DataCite attributes.
"""
