# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""oarepo-model customization registering the README export.

See [oarepo_formats.readme][] for the requirements (a DataCite JSON export on the model) and examples.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from invenio_i18n import lazy_gettext as _
from oarepo_model.customizations import AddMetadataExport

from .serializer import DEFAULT_TEMPLATE, ReadmeSerializer

if TYPE_CHECKING:
    from flask_babel.speaklater import LazyString

README_MIMETYPE = "text/markdown"
"""Mimetype of the README export, used for content negotiation (`Accept` header)."""

DEFAULT_NAME = _("README")
"""Default human-readable name of the export, shown on the landing page."""


class AddReadmeExport(AddMetadataExport):
    """Add a README (Markdown) export to the model.

    ```python
    customizations = [
        AddReadmeExport(),
    ]
    ```
    """

    def __init__(
        self,
        *,
        code: str = "readme",
        name: LazyString = DEFAULT_NAME,
        mimetype: str = README_MIMETYPE,
        extension: str = ".generated.md",
        template: str = DEFAULT_TEMPLATE,
        **kwargs: Any,
    ):
        """Create the customization.

        Args:
            code: code of the export.
            name: human-readable name of the export, shown on the landing page.
            mimetype: mimetype of the export, used for content negotiation (`Accept` header).
            extension: file extension of the export; `.generated.md` tells that the README was not
                written by the authors of the dataset.
            template: name of the Jinja template; a different name lets one model use its own template
                while the others keep the default one.
            **kwargs: any other `AddMetadataExport` / `Export` option, e.g. `display`.

        """
        super().__init__(
            code=code,
            name=name,
            mimetype=mimetype,
            serializer=ReadmeSerializer(template=template),
            extension=extension,
            **kwargs,
        )
