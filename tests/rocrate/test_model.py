# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of the oarepo-model customization."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from oarepo_formats.rocrate.exports.schema import DataCiteToROCrateSchema
from oarepo_formats.rocrate.model import AddROCrateExport

if TYPE_CHECKING:
    from oarepo_runtime.api import Export


def apply(customization) -> Export:
    exports: list[Export] = []
    builder = SimpleNamespace(get_list=lambda name: exports if name == "exports" else None)
    customization.apply(builder, None)
    (export,) = exports
    return export


def test_schema_and_export_options():
    """The options of the export reach it; the defaults are exercised by the api tests."""

    class Schema(DataCiteToROCrateSchema):
        pass

    export = apply(AddROCrateExport(code="crate", schema_cls=Schema, display=False))

    assert export.code == "crate"
    assert export.display is False
    assert type(export.serializer.object_schema) is Schema
