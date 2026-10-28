# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Blueprint making the README template available to the UI and API applications.

It has no views; it only adds `templates/` to the Jinja search path. Exports are served by the API
application, so the blueprint is registered under both `invenio_base.blueprints` and
`invenio_base.api_blueprints`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Blueprint

if TYPE_CHECKING:
    from flask import Flask


def create_blueprint(app: Flask) -> Blueprint:  # noqa: ARG001 - the invenio_base.blueprints signature
    """Create the blueprint with the README template."""
    return Blueprint("oarepo_formats_readme", __name__, template_folder="templates")
