# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of the two import decisions the REST tests can not reach with the one (drafts) test model.

Which deserializer the converted metadata goes to - including the escape hatch and a model without the
base import - and where the route is mounted for a model *without* drafts.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest
from flask import Flask, g
from marshmallow import ValidationError

from oarepo_formats.rocrate.imports import resource
from oarepo_formats.rocrate.imports.resource import IDENTITY_IMPORT, ROUTE, ROCrateImportConfig


class Deserializer:
    """A deserializer stub answering with its own name."""

    def __init__(self, name: str):
        """Create the stub."""
        self.name = name

    def deserialize(self, body: bytes) -> dict:
        """Answer which deserializer was used."""
        return {"by": self.name}


class RecordJSON:
    """The identity import's stub: the record JSON as a crate conversion could make it, foreign fields included."""

    def deserialize(self, body: bytes) -> dict:
        """Return record JSON with fields a crate must not set."""
        return {"metadata": {"title": "T"}, "custom_fields": {"x": 1}, "pids": {"doi": {}}, "access": {}}


IMPORTS = [
    SimpleNamespace(code="json", mimetype="application/json", deserializer=RecordJSON()),
    SimpleNamespace(code="datacite", mimetype="application/x-datacite", deserializer=Deserializer("datacite")),
]


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        pytest.param({"base_import": "datacite"}, {"by": "datacite"}, id="by-code"),
        pytest.param({"base_import": "application/x-datacite"}, {"by": "datacite"}, id="by-mimetype"),
        pytest.param({"deserializer": Deserializer("escape hatch")}, {"by": "escape hatch"}, id="escape-hatch"),
        pytest.param({"base_import": "application/vnd.datacite.datacite+json"}, ValueError, id="missing-import"),
        pytest.param(
            {"base_import": IDENTITY_IMPORT},
            {"metadata": {"title": "T"}, "custom_fields": {"x": 1}},
            id="the-identity-import-keeps-only-the-portable-fields",
        ),
    ],
)
def test_the_metadata_goes_to_the_configured_import(monkeypatch, config, expected):
    model = SimpleNamespace(get_import={key: i for i in IMPORTS for key in (i.code, i.mimetype)}.get)
    monkeypatch.setattr(resource, "current_runtime", SimpleNamespace(models={"test_model": model}))
    import_config = ROCrateImportConfig(**config, model_name="test_model")

    if expected is ValueError:
        with pytest.raises(ValueError, match="The model has no import"):
            import_config.deserialize(b"{}")
    else:
        assert import_config.deserialize(b"{}") == expected


@pytest.mark.parametrize(
    ("drafts", "rule"),
    [
        # the drafts resource keeps its blueprint prefix empty and prefixes its rules itself
        pytest.param(True, f"/api/model{ROUTE}", id="with-drafts"),
        pytest.param(False, ROUTE, id="without-drafts"),
    ],
)
def test_the_route_follows_the_resource_prefixing(monkeypatch, drafts, rule):
    class Base:
        def create_url_rules(self) -> list:
            return [{"rule": "the resource's own rule"}]

    class FakeResource(resource.ROCrateImportResourceMixin, Base):
        config = SimpleNamespace(url_prefix="/api/model")
        service = SimpleNamespace(id="model")
        decorators: ClassVar[list] = []

    monkeypatch.setattr(resource, "has_drafts", lambda service: drafts)

    assert [r["rule"] for r in FakeResource().create_url_rules()] == ["the resource's own rule", rule]


@pytest.mark.parametrize(
    ("identity_id", "refused"),
    [
        pytest.param(1, False, id="a-user"),
        pytest.param("7", False, id="a-user-id-as-text"),
        pytest.param("system", True, id="the-system-process"),
        pytest.param(None, True, id="anonymous"),
    ],
)
def test_files_written_by_a_task_need_a_user(identity_id, refused):
    """The task writes as the importing user; without one, the files would stay pending for good."""
    with Flask("test").test_request_context():
        g.identity = SimpleNamespace(id=identity_id)
        if refused:
            with pytest.raises(ValidationError, match="need a user"):
                resource._require_user()
        else:
            resource._require_user()
