# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""RO-Crate AIP export through the REST API: the `aip-access` gate and the full package."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import TYPE_CHECKING

import pytest
from flask_principal import AnonymousIdentity
from invenio_access.permissions import system_identity
from invenio_checks.models import CheckConfig, CheckRun, CheckRunStatus, Severity
from invenio_search.proxies import current_search_client

from oarepo_formats.rocrate.exports import aip
from oarepo_formats.rocrate.exports.aip import VERSIONS
from oarepo_formats.rocrate.model import ROCRATE_AIP_MIMETYPE
from tests.api_tests.conftest import exported

if TYPE_CHECKING:
    from pathlib import Path

    from tests.api_tests.conftest import Archive

AUDIT_LOG_INDEX = "auditlog-audit-log-v2.0.0"
"""Concrete index the audit log service writes to; it searches through the `auditlog` alias."""


@pytest.fixture(scope="module")
def app_config(app_config):
    """Enable the audit log and the curation checks: the AIP carries both."""
    app_config["AUDIT_LOGS_ENABLED"] = True
    app_config["CHECKS_ENABLED"] = True
    return app_config


@pytest.fixture(scope="module", autouse=True)
def _audit_log_index(search):
    """Create the audit log index with the package's mappings, and the alias the service reads through.

    A deployment runs `invenio index init` and gets a data stream (whose `@timestamp` is implicit) plus
    the alias; the test fixture creates a plain index, so `@timestamp` - the index field of the entry's
    `created` - is added to the mapping, and the maps are otherwise the package's (keyword
    `resource.type`, which the service's facet aggregation needs).
    """
    template = json.loads(
        files("invenio_audit_logs")
        .joinpath("records/mappings/templates/os-v2/auditlogs/datastream-auditlog-v2.0.0.json")
        .read_text()
    )
    mappings = template["template"]["mappings"]
    mappings["properties"]["@timestamp"] = {"type": "date"}
    client = current_search_client
    client.indices.delete(index="auditlog-*", ignore_unavailable=True)
    client.indices.delete(index="auditlog", ignore_unavailable=True)
    client.indices.create(
        index=AUDIT_LOG_INDEX,
        body={"settings": template["template"]["settings"], "mappings": mappings},
    )
    client.indices.put_alias(index=AUDIT_LOG_INDEX, name="auditlog")
    return search


def download_aip(client, url: str, tmp_path: Path) -> Archive:
    """Download the AIP; the audit log it carries is read from the index, so refresh that first."""
    current_search_client.indices.refresh(index="auditlog")
    return exported(client, url, ROCRATE_AIP_MIMETYPE, tmp_path)


def member(archive: Archive, name: str) -> dict:
    """Return a JSON member of the package (`metadata/repository/<name>`)."""
    return json.loads(archive.read(f"metadata/repository/{name}"))


def test_the_aip_export_is_listed_only_for_holders(formats_model, grant):
    """The landing page lists the bag for every reader and the AIP only for holders of `aip-access`."""
    exports = {export.code: export for export in formats_model.exports}
    holder = grant("aip-access")

    for identity in (None, AnonymousIdentity(), holder.identity):
        assert exports["rocrate-bagit"].is_displayed(identity) is True
    assert exports["rocrate-aip"].is_displayed(None) is False
    assert exports["rocrate-aip"].is_displayed(AnonymousIdentity()) is False
    assert exports["rocrate-aip"].is_displayed(holder.identity) is True


def test_the_aip_of_a_record(client, grant, db, service, record_url, record_with_file, tmp_path):
    """The full package: a valid bag with every member, each member's content, and the export recorded."""
    import jsonschema
    from invenio_audit_logs.records.models import AuditLog

    record = service.read(system_identity, record_with_file.id)._record
    config = CheckConfig(check_id="metadata", params={}, severity=Severity.FAIL, enabled=True)
    db.session.add(config)
    db.session.flush()
    db.session.add(
        CheckRun(
            config_id=config.id,
            record_id=record.id,
            is_draft=False,
            revision_id=1,
            status=CheckRunStatus.COMPLETED,
            state={},
            result={"errors": [{"message": "missing title"}], "warnings": []},
        )
    )
    db.session.commit()
    grant("aip-access")

    archive = download_aip(client, record_url, tmp_path)
    bag, graph = archive.bag, archive.graph

    # a valid, complete bag with every member
    assert bag.is_valid()
    assert bag.info["OARepo-Package-Level"] == "aip"
    assert bag.info["OARepo-Package-Complete"] == "true"
    assert sorted(bag.payload_files()) == sorted(
        [
            "data/README.generated.md",
            "data/metadata/dataset-v2.xml",
            "data/metadata/record.json",
            "data/metadata/repository/audit-log.json",
            "data/metadata/repository/checks.json",
            "data/metadata/repository/environment.json",
            "data/metadata/repository/files.json",
            "data/metadata/repository/requests.json",
            "data/metadata/repository/schema.json",
            "data/metadata/repository/versions.json",
            "data/data/data.csv",
            "data/ro-crate-metadata.json",
        ]
    )
    # ... each described in the crate
    parts = {reference["@id"] for reference in graph["./"]["hasPart"]}
    assert {"metadata/record.json", "metadata/repository/files.json", "metadata/repository/audit-log.json"} <= parts
    assert all(
        graph[path]["encodingFormat"] == "application/json" for path in parts if path.startswith("metadata/repository/")
    )

    # record.json is the system's view of the record, and valid against the package's schema
    record_json = json.loads(archive.read("metadata/record.json"))
    assert record_json == service.read(system_identity, record_with_file.id, expand=True).to_dict()
    jsonschema.Draft202012Validator.check_schema(member(archive, "schema.json"))
    jsonschema.validate(record_json, member(archive, "schema.json"))

    # files.json lists the payload files
    entries = member(archive, "files.json")["entries"]
    assert set(entries) == {"data.csv"}
    assert entries["data.csv"]["checksum"].startswith("md5:")
    assert entries["data.csv"]["status"] == "completed"

    # the audit log, as a member and as crate actions; this export is itself recorded
    log = member(archive, "audit-log.json")["entries"]
    assert {"draft.create", "record.publish"} <= {entry["action"] for entry in log}
    assert all(entry["resource"]["id"] == record_with_file.id for entry in log)
    assert {f"#audit-{entry['id']}" for entry in log} <= set(graph)
    assert bag.info["OARepo-Audit-Log"] == f"included ({len(log)} entries)"
    assert AuditLog.query.filter_by(action="record.aip_export").count() == 1

    # the curation checks, as a member and as an assessment action
    checks = member(archive, "checks.json")
    assert [run["status"] for run in checks["runs"]] == ["completed"]
    assert checks["configs"] == [
        {
            "id": str(config.id),
            "check_id": "metadata",
            "params": {},
            "severity": "error",
            "enabled": True,
            "community": {},
        }
    ]
    action = graph[f"#check-run-{checks['runs'][0]['id']}"]
    assert action["@type"] == "AssessAction"
    assert action["actionStatus"] == {"@id": "http://schema.org/CompletedActionStatus"}
    assert action["description"] == "1 errors, 0 warnings"
    assert bag.info["OARepo-Checks"] == "included (1 runs)"


def test_a_hidden_file_is_included_and_an_unfinished_upload_is_not(
    client, grant, db, search_clear, location, formats_model, service, draft_files_service, add_file, tmp_path
):
    draft = service.create(system_identity, {"metadata": {"title": "Test"}, "files": {"enabled": True}})
    add_file(draft.id, "shown.txt", b"shown")
    add_file(draft.id, "hidden.txt", b"hidden")
    draft_files_service.init_files(system_identity, draft.id, data=[{"key": "unfinished.txt"}])  # never committed
    file_record = next(
        file_record
        for file_record in draft_files_service.list_files(system_identity, draft.id)._results
        if file_record.key == "hidden.txt"
    )
    file_record.model.json = {**file_record.model.json, "access": {"hidden": True}}
    db.session.commit()
    formats_model.Draft.index.refresh()

    grant("aip-access")

    archive = download_aip(client, f"{formats_model.RecordResourceConfig.url_prefix}/{draft.id}/draft", tmp_path)

    payload = set(archive.bag.payload_files())
    assert {"data/data/shown.txt", "data/data/hidden.txt"} <= payload
    assert "data/data/unfinished.txt" not in payload
    entries = member(archive, "files.json")["entries"]
    assert set(entries) == {"shown.txt", "hidden.txt"}
    assert entries["hidden.txt"]["access"]["hidden"] is True
    assert {"data/shown.txt", "data/hidden.txt"} <= set(archive.graph)


def test_versions_member_holds_the_version_chain(
    client, grant, db, search_clear, location, formats_model, service, add_file, record_with_file, tmp_path
):
    draft = service.new_version(system_identity, record_with_file.id)
    add_file(draft.id, "v2.txt", b"v2")  # a files-enabled version needs a file to be published
    service.publish(system_identity, draft.id)
    formats_model.Record.index.refresh()
    url = f"{formats_model.RecordResourceConfig.url_prefix}/{draft.id}"

    grant("aip-access")

    versions = member(download_aip(client, url, tmp_path), "versions.json")["versions"]
    assert sorted(version["index"] for version in versions) == [1, 2]
    assert all(version["is_published"] for version in versions)


def test_a_failing_collector_leaves_the_member_out(
    client, grant, record_url, record_with_file, tmp_path, monkeypatch, caplog
):
    def failing(context):
        raise RuntimeError("versions are gone")

    monkeypatch.setattr(aip, "DEFAULT_AIP_COLLECTORS", (failing,))
    monkeypatch.setitem(aip.COLLECTOR_PATHS, failing, VERSIONS)

    grant("aip-access")

    archive = download_aip(client, record_url, tmp_path)

    assert archive.bag.is_valid()
    assert VERSIONS not in archive.names
    assert archive.bag.info["OARepo-Package-Complete"] == "false"
    action = archive.graph["#member-metadata-repository-versions.json-failed"]
    assert action["actionStatus"] == {"@id": "http://schema.org/FailedActionStatus"}
    assert action["error"] == "RuntimeError: versions are gone"
    assert "versions are gone" in caplog.text
