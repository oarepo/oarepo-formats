# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""RO-Crate export through the REST API of a real oarepo-model model."""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from oarepo_runtime.api import ExportEngine

from oarepo_formats.rocrate.model import ROCRATE_MIMETYPE
from tests.rocrate.documents import root_of


def test_export_via_accept_header(client, record_url):
    res = client.get(record_url, headers={"Accept": ROCRATE_MIMETYPE})

    assert res.status_code == 200, res.data
    assert res.mimetype == "application/ld+json"
    document = res.json
    graph = {e["@id"]: e for e in document["@graph"]}
    root = graph[graph["ro-crate-metadata.json"]["about"]["@id"]]
    # metadata comes from the DataCite export stub
    assert root["@id"] == "https://doi.org/10.82433/b09z-4k37"
    assert root["name"] == "Example Title"
    # files come from the record itself
    (part,) = root["hasPart"]
    assert part["@id"].endswith(f"{record_url}/files/data.csv/content")
    assert graph[part["@id"]]["contentSize"] == "8"
    # the checksum Invenio stores, as a Workflow Run RO-Crate term defined in the context
    assert graph[part["@id"]]["md5"] == hashlib.md5(b"a,b\n1,2\n", usedforsecurity=False).hexdigest()
    assert document["@context"] == [
        "https://w3id.org/ro/crate/1.3/context",
        {"md5": "https://w3id.org/ro/terms/workflow-run#md5"},
    ]
    # the @id of a web-based data entity must be a direct download (spec 7.4.3);
    # links carry the /api prefix of the deployed API app, the test client serves the API at the root
    download = client.get(urlparse(part["@id"]).path.removeprefix("/api"))
    assert download.status_code == 200
    assert download.data == b"a,b\n1,2\n"


def test_search_returns_one_crate_per_hit(client, formats_model, record_with_file):
    res = client.get(formats_model.RecordResourceConfig.url_prefix, headers={"Accept": ROCRATE_MIMETYPE})

    assert res.status_code == 200, res.data
    (document,) = res.json
    assert root_of(document)["@id"] == "https://doi.org/10.82433/b09z-4k37"


def test_datacite_export_is_shared_through_export_cache(client, formats_model, record_url):
    """In an export cache scope, the DataCite export the crate is converted from is serialized only once."""
    record = client.get(record_url, headers={"Accept": "application/json"}).json
    exports = {e.code: e for e in formats_model.exports}
    datacite_serializer, rocrate_serializer = exports["datacite"].serializer, exports["rocrate"].serializer
    calls = datacite_serializer.calls

    with ExportEngine.export_cache():
        rocrate_serializer.serialize_object(record)
        rocrate_serializer.serialize_object(record)

    assert datacite_serializer.calls == calls + 1
