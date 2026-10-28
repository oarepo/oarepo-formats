# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Blueprint making the RO-Crate import notification templates available to the UI application.

Celery renders the import's mails in the UI application (`invenio_app/celery.py`, `create_ui`), so the
templates have to be on its Jinja search path; the blueprint has no views, it only adds `templates/`. It is
registered under `invenio_base.blueprints` (and `invenio_base.api_blueprints`, so that a test or a
deployment that renders a mail in the API application finds them too), like `oarepo_formats.readme`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Blueprint

if TYPE_CHECKING:
    from flask import Flask


def create_blueprint(app: Flask) -> Blueprint:  # noqa: ARG001 - the invenio_base.blueprints signature
    """Create the blueprint with the import notification templates."""
    return Blueprint("oarepo_formats_rocrate", __name__, template_folder="templates")
