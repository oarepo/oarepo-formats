# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of the RO-Crate actions describing the record's history and the packaging."""

from __future__ import annotations

from flask import Flask

from oarepo_formats.rocrate.exports.provenance import FORMATS_URL, add_provenance
from oarepo_formats.rocrate.exports.zip import CrateContent

COMPLETED = {"@id": "http://schema.org/CompletedActionStatus"}
CREATED = "2026-01-01T00:00:00+00:00"
UPDATED = "2026-02-01T00:00:00+00:00"


def document() -> dict:
    return {"@graph": [{"@id": "ro-crate-metadata.json"}, {"@id": "./"}]}


def entities(doc: dict, *types: str) -> dict[str, dict]:
    return {e["@id"]: e for e in doc["@graph"] if e.get("@type") in types}


def test_actions_of_a_published_record():
    crate = CrateContent(document=document(), members=[], files=[], record=None)
    record = {
        "id": "abc",
        "created": CREATED,
        "updated": UPDATED,
        "is_published": True,
        "versions": {"index": 2},
        "metadata": {"publication_date": "2026-02-02"},
    }

    with Flask("test").app_context():
        add_provenance(crate, record)

    actions = entities(crate.document, "CreateAction", "UpdateAction")
    assert set(actions) == {"#record-created", "#version-published", "#record-modified", "#bag-created"}
    assert actions["#record-created"]["endTime"] == CREATED
    assert actions["#version-published"]["name"] == "Version 2 published"
    assert actions["#version-published"]["endTime"] == "2026-02-02"
    assert actions["#record-modified"]["endTime"] == UPDATED
    assert actions["#bag-created"]["result"] == {"@id": "ro-crate-metadata.json"}
    # a curation action must have an object, and the completed ones say so
    assert {action["object"]["@id"] for action in actions.values()} == {"./"}
    assert all(action["actionStatus"] == COMPLETED for action in actions.values())
    # no SITE_UI_URL in this app: the repository is not claimed as the instrument, oarepo-formats is
    assert "instrument" not in actions["#record-created"]
    assert actions["#bag-created"]["instrument"] == {"@id": FORMATS_URL}
    assert set(entities(crate.document, "SoftwareApplication")) == {FORMATS_URL}


def test_an_unchanged_record_has_no_modified_action():
    """`updated` equals `created` until something changes the record."""
    crate = CrateContent(document=document(), members=[], files=[], record=None)

    with Flask("test").app_context():
        add_provenance(crate, {"id": "abc", "created": CREATED, "updated": CREATED})

    assert "#record-modified" not in entities(crate.document, "CreateAction", "UpdateAction")


def test_the_repository_is_the_instrument_when_configured():
    app = Flask("test")
    app.config["SITE_UI_URL"] = "https://repo.example.org"
    app.config["THEME_SITENAME"] = "Repository"
    crate = CrateContent(document=document(), members=[], files=[], record=None)

    with app.app_context():
        add_provenance(crate, {"id": "abc", "created": CREATED})

    applications = entities(crate.document, "SoftwareApplication")
    assert applications["https://repo.example.org"] == {
        "@id": "https://repo.example.org",
        "@type": "SoftwareApplication",
        "name": "Repository",
        "url": "https://repo.example.org",
    }
    assert applications[FORMATS_URL]["version"]
    assert entities(crate.document, "CreateAction", "UpdateAction")["#record-created"]["instrument"] == {
        "@id": "https://repo.example.org"
    }


def test_the_owner_is_an_opaque_person_entity():
    """No name and no e-mail of the owner end up in the package."""
    crate = CrateContent(document=document(), members=[], files=[], record=None)

    with Flask("test").app_context():
        add_provenance(crate, {"id": "abc", "created": CREATED, "parent": {"access": {"owned_by": {"user": "42"}}}})

    owner = {"@id": "#owner-42", "@type": "Person", "identifier": "42"}
    assert entities(crate.document, "Person") == {"#owner-42": owner}
    assert entities(crate.document, "CreateAction")["#record-created"]["agent"] == {"@id": "#owner-42"}
