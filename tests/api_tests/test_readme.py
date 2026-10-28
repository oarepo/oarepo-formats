# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""README export through the REST API of a real oarepo-model model."""

from __future__ import annotations

from pathlib import Path

import pytest
from oarepo_runtime.api import ExportEngine, ExportRepresentation

from oarepo_formats.readme.model import README_MIMETYPE

ACCEPT_README = {"Accept": README_MIMETYPE}


def test_export_via_accept_header(client, record_url):
    res = client.get(record_url, headers=ACCEPT_README)

    assert res.status_code == 200, res.data
    assert res.mimetype == "text/markdown"
    readme = res.text
    # metadata comes from the DataCite export stub
    assert readme.startswith("# DATASET NAME: Example Title\n")
    # readers must not take it for a README written by the authors
    assert "> This README was **generated automatically** from the metadata" in readme
    landing_page = client.get(record_url, headers={"Accept": "application/json"}).json["links"]["self_html"]
    assert f"see the\n> record in the repository: [{landing_page}]({landing_page})." in readme
    assert "- PID: [https://doi.org/10.82433/b09z-4k37](https://doi.org/10.82433/b09z-4k37)" in readme
    assert "- Collection date: 2022-01-01 to 2022-12-31" in readme
    # files come from the record itself, with a download link
    assert f"{record_url}/files/data.csv/content) (8 Bytes" in readme
    # the credit required by the CC BY license of the README template
    assert "Collective of Data Stewards of the Czech Data Stewards Community" in readme


def test_download_file_name_says_generated(app, client, record_url):
    """The landing page downloads an export as `<id><extension>`."""
    record = client.get(record_url, headers={"Accept": "application/json"}).json

    with app.test_request_context():
        _, _, headers = ExportEngine.export(record, export_code="readme", representation=ExportRepresentation.RESPONSE)

    assert headers["Content-Disposition"] == f"attachment; filename={record['id']}.generated.md"


@pytest.fixture
def overridden_readme(app):
    """Install a README template of the repository's theme that replaces a piece and a section of the default one.

    It goes below the *app's* instance path: the `instance_path` fixture is per module, but the jinja loader
    of the instance templates may belong to an app created for an earlier module.
    """
    override = Path(app.instance_path) / "templates" / "semantic-ui" / "oarepo_formats" / "readme" / "README.md.jinja"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        '{% extends "oarepo_formats/readme/base.md.jinja" %}\n'
        # one piece of a section, using a value computed by the default template
        '{% block authors %}Authors: {{ creators | map(attribute="name") | join("; ") }}\n{% endblock %}\n'
        # a whole section
        "{% block data_structure_section %}\n## DATA STRUCTURE\n\nDescribed by the repository.\n{% endblock %}\n"
    )
    app.jinja_env.cache.clear()  # as after a restart: the default template may already be compiled
    yield
    override.unlink()
    app.jinja_env.cache.clear()


def test_template_is_overridden_by_the_theme(client, record_url, overridden_readme):
    """Invenio's override mechanism: <instance>/templates/<APP_THEME>/... wins over the package template."""
    readme = client.get(record_url, headers=ACCEPT_README).text

    assert "## DATA STRUCTURE\n\nDescribed by the repository.\n" in readme
    assert "Example TechnicalInfo" not in readme
    # only the authors are replaced, the rest of their section is kept
    assert "Authors: ExampleFamilyName, ExampleGivenName; ExampleOrganization\n" in readme
    assert "Author(s):" not in readme
    assert "- PID: [https://doi.org/10.82433/b09z-4k37]" in readme
    assert readme.startswith("# DATASET NAME: Example Title\n")  # the other sections are kept
