# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Fixtures for integration tests: pytest-invenio API app with the test model registered.

They need the docker services (`./run.sh test` starts them).
"""

from __future__ import annotations

import io
import json
import warnings
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import bagit
import pytest
from flask import Blueprint
from invenio_access import ActionUsers
from invenio_access.permissions import system_identity
from oarepo_runtime.services.records.mapping import update_all_records_mappings

from oarepo_formats.rocrate.model import ROCRATE_AIP_MIMETYPE, ROCRATE_BAGIT_MIMETYPE, ROCRATE_ZIP_MIMETYPE


@pytest.fixture(autouse=True)
def _quiet_bagit_deprecations():
    """Keep bagit's own deprecations out of the test report.

    bagit 1.9 warns about `codecs.open()` and about `re.sub(..., count)` on every validation. An
    `ignore` filter in `pyproject.toml` does not survive: pytest-flask's `deprecated()` decorator calls
    `warnings.simplefilter("default", DeprecationWarning)` when another module's app is created, which
    leaves a filter in front of it. Inserting the filter inside the test wins over that one.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning, module="bagit")
        yield


@pytest.fixture(scope="module")
def app(base_app, search, database):
    """Return the Invenio application with database and search.

    This is pytest-invenio's `app`; the XSLT unit tests (`tests/xslt`) use a bare Flask app instead.
    """
    return base_app


@pytest.fixture(scope="module")
def create_app(instance_path, entry_points):
    """Application factory fixture."""
    from invenio_app.factory import create_api

    return create_api


def create_rdm_ui_blueprint(app):
    """Create a fake `invenio_app_rdm_records` UI blueprint; the drafts preset links to its endpoints."""
    blueprint = Blueprint("invenio_app_rdm_records", __name__)
    for rule, endpoint in [
        ("/records/<pid_value>", "record_detail"),
        ("/records/<pid_value>/latest", "record_latest"),
        ("/records/<pid_value>/files/<path:filename>", "record_file_download"),
        ("/uploads/<pid_value>", "deposit_edit"),
        ("/pid_scheme/<path:pid_value>", "record_from_pid"),
    ]:
        blueprint.add_url_rule(rule, endpoint, lambda **kwargs: "")
    return blueprint


@pytest.fixture(scope="module")
def extra_entry_points(formats_model):
    """Build (and register) the test model before the app is created; add the fake UI blueprint."""
    return {"invenio_base.blueprints": [f"formats_test_ui = {__name__}:create_rdm_ui_blueprint"]}


@pytest.fixture(scope="module")
def app_config(app_config):
    """Override pytest-invenio app_config."""
    app_config["FILES_REST_STORAGE_CLASS_LIST"] = {"L": "Local"}
    app_config["FILES_REST_DEFAULT_STORAGE_CLASS"] = "L"
    app_config["RECORDS_REFRESOLVER_CLS"] = "invenio_records.resolver.InvenioRefResolver"
    app_config["RECORDS_REFRESOLVER_STORE"] = "invenio_jsonschemas.proxies.current_refresolver_store"
    app_config["REST_CSRF_ENABLED"] = False
    app_config["APP_THEME"] = ["semantic-ui"]  # templates are looked up under the theme prefix first
    return app_config


@pytest.fixture(scope="module")
def search(search):
    """Search fixture with the model mappings."""
    update_all_records_mappings()
    return search


@pytest.fixture
def service(app):
    """Return the records service of the test model."""
    return app.extensions["formats_test"].records_service


@pytest.fixture
def draft_files_service(app):
    """Return the draft files service of the test model."""
    return app.extensions["formats_test"].draft_files_service


@pytest.fixture
def add_file(draft_files_service):
    """Add a committed file to a draft."""

    def _add_file(draft_id: str, key: str, content: bytes) -> None:
        draft_files_service.init_files(system_identity, draft_id, data=[{"key": key}])
        draft_files_service.set_file_content(system_identity, draft_id, key, BytesIO(content))
        draft_files_service.commit_file(system_identity, draft_id, key)

    return _add_file


@pytest.fixture
def draft_with_file(app, db, search_clear, location, formats_model, service, add_file):
    """Return a draft with the file `data.csv`."""
    draft = service.create(system_identity, {"metadata": {"title": "Test"}, "files": {"enabled": True}})
    add_file(draft.id, "data.csv", b"a,b\n1,2\n")
    formats_model.Draft.index.refresh()
    return draft


@pytest.fixture
def record_with_file(formats_model, service, draft_with_file):
    """Return a published record with the file `data.csv`."""
    record = service.publish(system_identity, draft_with_file.id)
    formats_model.Record.index.refresh()
    return record


@pytest.fixture
def record_url(formats_model, record_with_file):
    """Return the API URL of the published record, relative to the API app."""
    return f"{formats_model.RecordResourceConfig.url_prefix}/{record_with_file.id}"


@pytest.fixture
def import_client(client, UserFixture, app, db):  # noqa: N803 the factory is named like pytest-invenio's
    """Return the test client, logged in as a depositor - creating a draft needs a user."""
    user = UserFixture(email="importer@example.org", password="password", active=True, confirmed=True)  # noqa: S106
    user.create(app, db)
    user.login(client)
    return client


@pytest.fixture
def records_url(formats_model) -> str:
    """Return the URL prefix of the test model; the import route is below it."""
    return formats_model.RecordResourceConfig.url_prefix


@pytest.fixture
def grant(client, UserFixture, app, db):  # noqa: N803 the factory is named like pytest-invenio's
    """Return a function that logs the test client in as a user holding an access action, and returns the user."""

    def _grant(action: str):
        user = UserFixture(email=f"{action}@example.org", password="password", active=True, confirmed=True)  # noqa: S106
        user.create(app, db)
        db.session.add(ActionUsers.allow(app.extensions["invenio-access"].actions[action], user_id=user.user.id))
        db.session.commit()
        user.login(client)
        return user

    return _grant


#
# the archive exports
#
ARCHIVE_NAMES = {
    ROCRATE_ZIP_MIMETYPE: ".zip",
    ROCRATE_BAGIT_MIMETYPE: "-bagit.zip",
    ROCRATE_AIP_MIMETYPE: "-aip.zip",
}
"""The download name of each archive export: `<record id><suffix>`."""


@dataclass
class Archive:
    """One downloaded archive export: a plain RO-Crate ZIP, a BagIt bag, or an AIP (a bag)."""

    zip: zipfile.ZipFile
    crate_root: str
    """Where the crate is: `""` in a plain ZIP, `"<id>/data/"` in a bag."""
    bag: bagit.Bag | None
    """The extracted bag, for `bagit` to validate; None for a plain ZIP."""

    @property
    def names(self) -> list[str]:
        """The members of the crate, relative to the crate root, in archive order."""
        return [name[len(self.crate_root) :] for name in self.zip.namelist() if name.startswith(self.crate_root)]

    def read(self, path: str) -> bytes:
        """Return a member of the crate, by its path relative to the crate root."""
        return self.zip.read(f"{self.crate_root}{path}")

    @property
    def metadata(self) -> dict:
        """The crate's `ro-crate-metadata.json`."""
        return json.loads(self.read("ro-crate-metadata.json"))

    @property
    def graph(self) -> dict[str, dict]:
        """The crate's entities by `@id`."""
        return {entity["@id"]: entity for entity in self.metadata["@graph"]}

    def bag_path(self, relative: str) -> Path:
        """Return a file of the extracted bag (`bag-info.txt`, `manifest-sha512.txt`, `data/...`)."""
        assert self.bag is not None
        return Path(str(self.bag.path)) / relative


def exported(client, url: str, accept: str, tmp_path: Path | None = None) -> Archive:
    """Download an archive export, check the contract every archive response keeps, and open it.

    A bag (BagIt, AIP) is extracted into `tmp_path` too, so that `bagit` can validate it.
    """
    response = client.get(url, headers={"Accept": accept})
    assert response.status_code == 200, response.data
    assert response.mimetype == accept.split(";")[0]  # the profile parameter is not echoed
    assert int(response.headers["Content-Length"]) == len(response.data)
    disposition = response.headers["Content-Disposition"]
    assert disposition.startswith("attachment; filename=")
    assert disposition.endswith(ARCHIVE_NAMES[accept])
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    bag_dir = next((name.removesuffix("bagit.txt") for name in archive.namelist() if name.endswith("/bagit.txt")), None)
    if bag_dir is None:
        return Archive(archive, "", None)
    assert tmp_path is not None, "a bag is validated on disk: pass tmp_path"
    archive.extractall(tmp_path)
    return Archive(archive, f"{bag_dir}data/", bagit.Bag(str(tmp_path / bag_dir)))
