# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""XSLT export and import through the REST API of a real oarepo-model model."""

from __future__ import annotations

import pytest

from tests.conftest import DATASET_V2_XML_MIMETYPE, DATASET_XML_MIMETYPE


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        pytest.param(DATASET_XML_MIMETYPE, "<dataset><name>Test</name></dataset>", id="dataset-xml"),
        # dataset-v2-xml transforms the output of the dataset-xml export, itself an XSLT export
        pytest.param(
            DATASET_V2_XML_MIMETYPE, '<dataset version="2"><title>Test</title></dataset>', id="on-a-base-export"
        ),
    ],
)
def test_an_xslt_export(client, record_url, accept, expected):
    res = client.get(record_url, headers={"Accept": accept})

    assert res.status_code == 200, res.data
    assert res.text == expected


def test_deployment_replaces_export_stylesheet(app, client, record_url, monkeypatch):
    """`AddXSLTExport` names its stylesheet `export:<code>`, which invenio.cfg can point elsewhere."""
    monkeypatch.setitem(
        app.config, "OAREPO_XSLT_STYLESHEETS", {"export:dataset-xml": "tests.api_tests.data:replaced.xslt"}
    )

    res = client.get(record_url, headers={"Accept": DATASET_XML_MIMETYPE})

    assert res.text == "<replaced/>"


def post(client, formats_model, content_type: str, body: str):
    return client.post(
        formats_model.RecordResourceConfig.url_prefix,
        data=body,
        headers={"Content-Type": content_type, "Accept": "application/json"},
    )


@pytest.mark.parametrize(
    ("content_type", "body", "title"),
    [
        pytest.param(DATASET_XML_MIMETYPE, "<dataset><name>Imported</name></dataset>", "Imported", id="dataset-xml"),
        # dataset-v2-xml transforms the body and hands it to the dataset-xml import, itself an XSLT import
        pytest.param(
            DATASET_V2_XML_MIMETYPE,
            '<dataset version="2"><title>Imported v2</title></dataset>',
            "Imported v2",
            id="handed-to-a-base-import",
        ),
    ],
)
def test_an_xslt_import_creates_a_draft(client, formats_model, db, search_clear, content_type, body, title):
    res = post(client, formats_model, content_type, body)

    assert res.status_code == 201, res.data
    assert res.json["metadata"]["title"] == title


@pytest.mark.parametrize(
    ("body", "error"),
    [
        # xsl:message terminate="yes" is how a stylesheet rejects a document it does not understand
        ("<other/>", "Not a dataset: other"),
        ("<dataset>", "Invalid XML"),
    ],
    ids=["rejected-by-stylesheet", "not-well-formed"],
)
def test_import_error_is_bad_request(client, formats_model, db, search_clear, body, error):
    res = post(client, formats_model, DATASET_XML_MIMETYPE, body)

    assert res.status_code == 400
    assert error in res.text


def test_import_does_not_expand_external_entities(client, formats_model, db, search_clear, tmp_path):
    """An uploaded document must not be able to read server files (XXE)."""
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret")
    res = post(
        client,
        formats_model,
        DATASET_XML_MIMETYPE,
        f'<!DOCTYPE dataset [<!ENTITY x SYSTEM "{secret.as_uri()}">]><dataset><name>&x;</name></dataset>',
    )

    assert "top-secret" not in res.text
