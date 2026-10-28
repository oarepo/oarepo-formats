# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of the `aip-access` permission generator, without an application."""

from __future__ import annotations

from flask_principal import Identity
from invenio_search.engine import dsl

from oarepo_formats.rocrate.permissions import AIPAccess, aip_access_action


def test_aip_access_needs_the_action_and_filters_nothing_out_for_a_holder():
    identity = Identity(id=1)

    assert AIPAccess().needs() == [aip_access_action]
    assert AIPAccess().query_filter(identity=identity) is None

    identity.provides.add(aip_access_action)

    assert AIPAccess().query_filter(identity=identity) == dsl.Q("match_all")
