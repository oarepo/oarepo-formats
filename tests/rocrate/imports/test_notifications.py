# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of the RO-Crate import mail templates: each type renders its subject, html, plain and markdown.

The templates are rendered here with a hand-made context, without a database or a mail backend: they are
Jinja blocks (`subject`, `html_body`, `plain_body`, `md_body`) that the e-mail backend of
invenio-notifications renders separately. `tests/api_tests/test_rocrate_import.py` checks the same
templates through `mail.record_messages()`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import Flask
from invenio_app.helpers import ThemeJinjaLoader

from oarepo_formats.rocrate.views import create_blueprint

RECORD = {
    "id": "abc",
    "title": "Example Title",
    "self_html": "https://example.org/records/abc",
    "is_published": True,
    "is_draft": False,
}
"""What `snapshot()` puts into the notification context."""


def _ngettext(singular: str, plural: str, number: int, **variables: object) -> str:
    """Stand in for flask-babel's `ngettext` (a passthrough, like `_` below)."""
    return (singular if number == 1 else plural) % variables


@pytest.fixture
def jinja():
    """Return the Jinja environment with the package's templates and passthrough translations.

    The templates are looked up the way the mail backend does it (`invenio_notifications/<type>.jinja`),
    through the same theme loader a real application has (`APP_THEME`), so `{% extends %}` inside them
    resolves here as it does in production.
    """
    app = Flask("test")
    app.config["APP_THEME"] = ["semantic-ui"]
    app.register_blueprint(create_blueprint(app))
    app.jinja_env.loader = ThemeJinjaLoader(app, app.jinja_env.loader)
    app.jinja_env.globals["_"] = lambda text, **kwargs: text
    app.jinja_env.globals["ngettext"] = _ngettext
    return app.jinja_env


def render(jinja, type_: str, **context) -> str:
    """Render the whole template of one mail type, as the mail backend selects it."""
    template = jinja.get_template(f"invenio_notifications/{type_}.jinja")
    return template.render(notification=SimpleNamespace(context={"record": RECORD, **context}))


def render_block(jinja, type_: str, block: str, **context) -> str:
    """Render one block the way the mail backend does: the whole template first, then the block."""
    template = jinja.get_template(f"invenio_notifications/{type_}.jinja")
    render_context = template.new_context({"notification": SimpleNamespace(context={"record": RECORD, **context})})
    list(template.root_render_func(render_context))  # runs the top-level {% set %} of the template
    return "".join(template.blocks[block](render_context))


FAILED_FILE = {"key": "data.csv", "status": "failed", "error": "not found"}


@pytest.mark.parametrize(
    ("type_", "context", "present", "absent"),
    [
        pytest.param(
            "completed",
            {"files": [{"key": "a.csv"}, {"key": "b.csv"}]},
            [
                "RO-Crate import finished: Example Title",
                "All 2 files of 'Example Title' were downloaded and the record is ready.",
                "https://example.org/records/abc",
            ],
            [],
            id="completed-plural",
        ),
        pytest.param(
            "completed",
            {"files": [{"key": "a.csv"}]},
            ["All 1 file of 'Example Title' was downloaded and the record is ready."],
            [],
            id="completed-singular",
        ),
        pytest.param(
            "published",
            {"files": [{"key": "a.csv"}]},
            [
                "RO-Crate import published: Example Title",
                "All 1 file of 'Example Title' was downloaded and the record was published.",
                "https://example.org/records/abc",
            ],
            [],
            id="published",
        ),
        pytest.param(
            "failed",
            {"files": [FAILED_FILE], "reason": "files", "error": None},
            [
                "RO-Crate import failed: Example Title",
                "The import of 'Example Title' did not finish: a file could not be fetched.",
                "data.csv",
                "not found",
            ],
            [],
            id="failed-files",
        ),
        pytest.param(
            "failed",
            {"record": {**RECORD, "self_html": None}, "files": [], "reason": "publish", "error": "Refused"},
            ["did not finish: the record could not be published.", "Refused"],
            ["Open the record"],
            id="failed-publish-without-a-link",
        ),
        pytest.param(
            "failed",
            {"files": [], "reason": "error", "error": "boom"},
            ["an unexpected error stopped the import", "boom"],
            [],
            id="failed-unexpected-error",
        ),
    ],
)
def test_the_mail_text(jinja, type_, context, present, absent):
    rendered = render(jinja, f"rocrate-import.{type_}", **context)

    for text in present:
        assert text in rendered
    for text in absent:
        assert text not in rendered


def test_the_html_part_escapes_third_party_text(jinja):
    """The title comes from the crate and the error from a remote host: neither may be rendered as html."""
    html = render_block(
        jinja,
        "rocrate-import.failed",
        "html_body",
        record={**RECORD, "title": '<script>alert("title")</script>'},
        files=[{"key": "a.csv", "status": "failed", "error": "<script>alert('error')</script>"}],
        reason="files",
        error="<img src=x onerror=alert(1)>",
    )

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html
