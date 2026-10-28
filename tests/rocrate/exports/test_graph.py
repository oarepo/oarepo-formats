# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of serializing the crate metadata document and its parts."""

from __future__ import annotations

import json

from flask_babel.speaklater import LazyString

from oarepo_formats.rocrate.exports.graph import to_json_bytes


def test_lazy_string_is_serialized():
    """`THEME_SITENAME` is a `LazyString` (provenance's `SoftwareApplication` name); the crate has to stay JSON."""
    document = {"@graph": [{"@type": "SoftwareApplication", "name": LazyString(lambda: "InvenioRDM")}]}

    assert json.loads(to_json_bytes(document))["@graph"][0]["name"] == "InvenioRDM"
