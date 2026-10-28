# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of choosing the record's PID (`OAREPO_FORMATS_PID_SCHEMES`)."""

from __future__ import annotations

import pytest
from flask import Flask

from oarepo_formats.datacite import primary_pid
from oarepo_formats.ext import OARepoFormats
from oarepo_formats.rocrate.exports.schema import DataCiteToROCrateSchema

HANDLE = {"identifier": "21.12345/abc-1", "identifierType": "Handle"}
DATACITE = {"doi": "10.1234/x", "identifiers": [HANDLE, {"identifier": "ark:/12025/654xz321", "identifierType": "ARK"}]}
RECORD = {"links": {"self_html": "https://repo/r/1"}}


@pytest.fixture
def app_with_schemes():
    def make(schemes):
        app = Flask("test")
        app.config["OAREPO_FORMATS_PID_SCHEMES"] = schemes
        OARepoFormats(app)
        return app

    return make


def test_the_doi_is_the_pid_by_default():
    assert primary_pid(RECORD, DATACITE) == ("10.1234/x", "DOI")
    assert primary_pid(RECORD, {"identifiers": [HANDLE]}) is None


@pytest.mark.parametrize(
    ("schemes", "datacite", "expected"),
    [
        pytest.param(["Handle", "DOI"], DATACITE, ("21.12345/abc-1", "Handle"), id="first-in-the-list-wins"),
        pytest.param(["ark", "DOI"], DATACITE, ("ark:/12025/654xz321", "ARK"), id="case-insensitive"),
        pytest.param(["DOI", "Handle"], {"identifiers": [HANDLE]}, ("21.12345/abc-1", "Handle"), id="first-found"),
        pytest.param(["URN"], DATACITE, None, id="none-found"),
        pytest.param(lambda record, datacite: ["Handle"], DATACITE, ("21.12345/abc-1", "Handle"), id="callable"),
    ],
)
def test_the_configured_schemes_choose_the_pid(app_with_schemes, schemes, datacite, expected):
    with app_with_schemes(schemes).app_context():
        assert primary_pid(RECORD, datacite) == expected


def test_the_crate_is_identified_by_the_pid(app_with_schemes):
    with app_with_schemes(["Handle", "DOI"]).app_context():
        root = DataCiteToROCrateSchema().dump({"datacite": DATACITE, "record": {**RECORD, "files": {"entries": {}}}})
    assert root["@id"] == root["cite-as"] == "https://hdl.handle.net/21.12345/abc-1"
