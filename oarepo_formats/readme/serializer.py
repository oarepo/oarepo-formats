# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Serializer rendering a README (Markdown) of a record from a Jinja template.

[AddReadmeExport][oarepo_formats.readme.model.AddReadmeExport] creates it, so it is normally not used
directly. To change the README, override the template (see [oarepo_formats.readme][]); to give the
template more data, subclass the serializer and extend
[context][oarepo_formats.readme.serializer.ReadmeSerializer.context].
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from flask import render_template
from flask_resources.serializers import BaseSerializer
from oarepo_runtime.api import ExportEngine, ExportRepresentation
from werkzeug.exceptions import NotAcceptable

from ..datacite import DATACITE_JSON_MIMETYPE, pid_url, primary_pid
from ..files import file_entities

DEFAULT_TEMPLATE = "oarepo_formats/readme/README.md.jinja"
"""Name of the README template; override it in the instance's `templates` folder, see [oarepo_formats.readme][]."""


class ReadmeSerializer(BaseSerializer):
    """Render the README of a record from its DataCite metadata and its files.

    The record is exported to DataCite JSON by the DataCite export registered on its model, through
    `ExportEngine`, so in an export cache scope the DataCite export is shared with other exports.
    """

    def __init__(self, template: str = DEFAULT_TEMPLATE):
        """Create the serializer rendering the given template."""
        self.template = template

    def serialize_object(self, obj: dict) -> str:
        """Render the README of a serialized record.

        Raises:
            ValueError: if the record's model has no DataCite JSON export.

        """
        datacite = ExportEngine.export(
            obj,
            export_mimetype=DATACITE_JSON_MIMETYPE,
            representation=ExportRepresentation.DICTIONARY,
        )
        rendered = render_template(self.template, **self.context(obj, datacite))
        # templates need no whitespace control: runs of blank lines are collapsed to one
        return re.sub(r"\n\s*\n(\s*\n)+", "\n\n", rendered).strip() + "\n"

    def context(self, record: dict, datacite: dict) -> dict[str, Any]:
        """Return the variables of the template.

        - `datacite`, `record`: the arguments,
        - `files`: the files the user may download, as RO-Crate `File` entities (`name`, `contentSize`,
          `encodingFormat`, `@id` - the download URL),
        - `pid`: the URL of the record's PID (its DOI by default, see `OAREPO_FORMATS_PID_SCHEMES`), or the
          landing page if the record has none,
        - `landing_page`: the URL of the record's landing page (`links.self_html`), if known,
        - `pid_url`: `pid_url(value, scheme)` turns an identifier (ORCID, ROR, DOI, ...) into a URL,
        - `generated_on`: today's date (ISO 8601).

        Args:
            record: the serialized record (API JSON).
            datacite: the record's DataCite attributes.

        """
        pid = primary_pid(record, datacite)
        landing_page = (record.get("links") or {}).get("self_html")
        return {
            "datacite": datacite,
            "record": record,
            "files": file_entities(record),
            "pid": (pid_url(*pid) if pid else None) or landing_page,
            "landing_page": landing_page,
            "pid_url": pid_url,
            "generated_on": dt.datetime.now(dt.UTC).date().isoformat(),
        }

    def serialize_object_list(self, obj_list: Any) -> Any:  # noqa: ARG002 always refused
        """Refuse search results: a README describes one dataset."""
        raise NotAcceptable("The README export is available for single records only.")
