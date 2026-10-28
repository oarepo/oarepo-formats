# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""oarepo-model customizations registering the RO-Crate exports.

See [oarepo_formats.rocrate][] for the requirements (a DataCite JSON export on the model) and examples.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from invenio_i18n import lazy_gettext as _
from oarepo_model.customizations import AddMetadataExport, Customization, PrependMixin

from ..datacite import DATACITE_JSON_MIMETYPE
from .exports.aip import ROCrateAIPSerializer
from .exports.bag import BAGIT_INCLUDED_EXPORTS, ROCrateBagItSerializer
from .exports.schema import DataCiteToROCrateSchema
from .exports.serializer import ROCrateSerializer
from .exports.zip import DEFAULT_INCLUDED_EXPORTS, ROCrateZipSerializer
from .imports import ROCrateImportConfig, ROCrateImportResourceMixin
from .imports.crate_datacite import ROCrateToDataCiteSchema
from .permissions import has_aip_access

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import flask_principal
    from flask_babel.speaklater import LazyString
    from marshmallow import Schema
    from oarepo_model.builder import InvenioModelBuilder
    from oarepo_model.model import InvenioModel

ROCRATE_MIMETYPE = 'application/ld+json; profile="https://w3id.org/ro/crate"'
"""Mimetype of the RO-Crate export, used for content negotiation (`Accept` header).

The spec uses the unversioned profile URI for content negotiation (spec 7.7.5). The value must be quoted
and use `; ` - werkzeug normalizes `Accept` headers to that form (an unquoted URI is cut at the first
`:`) and flask-resources matches the result by exact string comparison, so a client sending the
unquoted form shown in the spec does not select the export.
"""

ROCRATE_ZIP_MIMETYPE = 'application/zip; profile="https://w3id.org/ro/crate"'
"""Mimetype of the RO-Crate ZIP export; the quoting rules of `ROCRATE_MIMETYPE` apply.

Plain `application/zip` is not claimed, so as not to shadow other ZIP exports.
"""

ROCRATE_BAGIT_MIMETYPE = 'application/zip; profile="https://www.rfc-editor.org/rfc/rfc8493"'
"""Mimetype of the BagIt export: a ZIP archive with a BagIt bag (RFC 8493).

The quoting rules of `ROCRATE_MIMETYPE` apply. Separate from `ROCRATE_ZIP_MIMETYPE`, so a client
selects one or the other by its `Accept` header.
"""

DEFAULT_NAME = _("RO-Crate")
"""Default human-readable name of the export, shown on the landing page."""

DEFAULT_ZIP_NAME = _("RO-Crate (ZIP with files)")
"""Default human-readable name of the ZIP export, shown on the landing page."""

DEFAULT_BAGIT_NAME = _("RO-Crate (BagIt)")
"""Default human-readable name of the BagIt export, shown on the landing page."""

ROCRATE_AIP_MIMETYPE = "application/vnd.oarepo.aip+zip"
"""Mimetype of the AIP export: a zipped BagIt bag with the record's full preservation package.

A vendor type, because content negotiation needs a mimetype of its own per export. `AddROCrateAIPExport(mimetype=...)`
changes it.
"""

DEFAULT_AIP_NAME = _("RO-Crate (AIP)")
"""Default human-readable name of the AIP export; listed on the landing page for holders of `aip-access`."""


class AddROCrateExport(AddMetadataExport):
    """Add a detached RO-Crate (JSON-LD metadata document) export to the model.

    ```python
    customizations = [
        AddROCrateExport(),
        # or with a customized mapping
        AddROCrateExport(
            schema_cls=MySchema
        ),
    ]
    ```
    """

    def __init__(
        self,
        *,
        code: str = "rocrate",
        name: LazyString = DEFAULT_NAME,
        mimetype: str = ROCRATE_MIMETYPE,
        extension: str = ".json",
        schema_cls: type[DataCiteToROCrateSchema] = DataCiteToROCrateSchema,
        **kwargs: Any,
    ):
        """Create the customization.

        `schema_cls` converts DataCite to nested schema.org JSON-LD; pass a subclass of
        [DataCiteToROCrateSchema][oarepo_formats.rocrate.exports.schema.DataCiteToROCrateSchema] to change or add a
        mapping. The other arguments and `kwargs` (any `AddMetadataExport` / `Export` option, e.g.
        `display`) configure the export itself.
        """
        super().__init__(
            code=code,
            name=name,
            mimetype=mimetype,
            serializer=ROCrateSerializer(schema_cls=schema_cls),
            extension=extension,
            **kwargs,
        )


class AddROCrateZipExport(AddMetadataExport):
    """Add an export streaming the record as a ZIP archive: an attached RO-Crate with all its files.

    The archive contains `ro-crate-metadata.json` (the same metadata as `AddROCrateExport`, with the
    files referenced by their paths in the archive) and every file the user may read. Permissions and
    `RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE` are checked as for Invenio's `/files-archive`, before
    anything is streamed.

    ```python
    customizations = [
        AddROCrateExport(
            schema_cls=MySchema
        ),
        AddROCrateZipExport(
            schema_cls=MySchema
        ),  # the same metadata in both
    ]
    ```

    The archive also contains the output of the model's other exports listed in `include_exports` (by
    default the README, DataCite XML, Dublin Core and schema.org JSON-LD, each if the model has it), and
    declares the profile `OAREPO_ROCRATE_PROFILE` if it is configured. See [oarepo_formats.rocrate.exports.zip][].

    Requires an oarepo-runtime with streaming exports (`Export.streaming`).
    """

    def __init__(  # noqa: PLR0913 - the export options plus the mapping and the included exports
        self,
        *,
        code: str = "rocrate-zip",
        name: LazyString = DEFAULT_ZIP_NAME,
        mimetype: str = ROCRATE_ZIP_MIMETYPE,
        extension: str = ".zip",
        schema_cls: type[DataCiteToROCrateSchema] = DataCiteToROCrateSchema,
        include_exports: Mapping[str, str] = DEFAULT_INCLUDED_EXPORTS,
        **kwargs: Any,
    ):
        """Create the customization; the arguments are the same as for `AddROCrateExport`, plus `include_exports`.

        `include_exports` maps export codes to archive paths: the model's exports added to the archive,
        each if the model has it (`{}` adds none).
        """
        super().__init__(
            code=code,
            name=name,
            mimetype=mimetype,
            serializer=ROCrateZipSerializer(schema_cls=schema_cls, include_exports=include_exports),
            extension=extension,
            streaming=True,
            **kwargs,
        )


class AddROCrateBagItExport(AddMetadataExport):
    """Add an export streaming the record as a zipped BagIt bag whose payload is the attached RO-Crate.

    The bag is a normal download: it is offered on the landing page next to the other exports and served
    by content negotiation like the RO-Crate ZIP. `display=False` keeps it out of the landing page, for a
    deployment that only wants it on request.

    ```python
    customizations = [
        AddROCrateExport(),
        AddROCrateZipExport(),
        AddROCrateBagItExport(),
    ]
    ```

    The package is the RO-Crate ZIP's content inside a BagIt bag (`data/` of the bag is the RO-Crate
    root), with MD5 and SHA-512 manifests, a tag manifest and provenance actions. It is an AIP of a
    record version when the caller may read the record's files and the version is published; the bag
    records the record state. See [oarepo_formats.rocrate.exports.bag][].

    Args:
        extension: appended to the record's id in the download name; the oarepo-ui export route names
            the download `<id><extension>`, so the serializer does the same.

    Requires an oarepo-runtime with streaming exports (`Export.streaming`).

    """

    def __init__(  # noqa: PLR0913 - the export options plus the mapping, the exports and the name
        self,
        *,
        code: str = "rocrate-bagit",
        name: LazyString = DEFAULT_BAGIT_NAME,
        mimetype: str = ROCRATE_BAGIT_MIMETYPE,
        extension: str = "-bagit.zip",
        schema_cls: type[DataCiteToROCrateSchema] = DataCiteToROCrateSchema,
        include_exports: Mapping[str, str] = BAGIT_INCLUDED_EXPORTS,
        display: bool = True,
        **kwargs: Any,
    ):
        """Create the customization; the arguments are the same as for `AddROCrateZipExport`, plus `display`.

        `include_exports` defaults to the RO-Crate ZIP's exports plus the record's own JSON
        (`metadata/record.json`). `display=False` keeps the export out of the landing page.
        """
        super().__init__(
            code=code,
            name=name,
            mimetype=mimetype,
            serializer=ROCrateBagItSerializer(
                schema_cls=schema_cls, include_exports=include_exports, extension=extension
            ),
            extension=extension,
            display=display,
            streaming=True,
            **kwargs,
        )


class AddROCrateAIPExport(AddMetadataExport):
    """Add the RO-Crate AIP export: the record's full Archival Information Package.

    It is the BagIt export extended with everything the repository knows about the record - the complete
    expanded record, all files including hidden ones, the file metadata, the versions, the requests with
    their timelines, the curation checks, the audit log, the record's JSON Schema and the software
    environment - and it has no download size limit. It is available only to the holders of `aip-access`
    (see [oarepo_formats.rocrate.permissions][]) and answers 403 to everyone else.

    The landing page lists it **only for those holders**: its `display_if` is
    [has_aip_access][oarepo_formats.rocrate.permissions.has_aip_access], which oarepo-ui's record-detail
    sidebar evaluates per request (`Export.is_displayed`). `display=False` keeps it off the landing page for
    everybody, and the export stays reachable by code or `Accept` header in both cases.

    ```python
    customizations = [
        AddROCrateExport(),
        AddROCrateZipExport(),
        AddROCrateBagItExport(),
        AddROCrateAIPExport(),
    ]
    ```

    The export does not grant access to a record the holder can not open: add `AIPAccess()` to the
    model's permission policy (see [oarepo_formats.rocrate.permissions][]). See
    [oarepo_formats.rocrate.exports.aip][] for the package and its collectors.

    Requires an oarepo-runtime with streaming exports (`Export.streaming`).

    """

    def __init__(  # noqa: PLR0913 - the export options plus the mapping, the exports and the name
        self,
        *,
        code: str = "rocrate-aip",
        name: LazyString = DEFAULT_AIP_NAME,
        mimetype: str = ROCRATE_AIP_MIMETYPE,
        extension: str = "-aip.zip",
        schema_cls: type[DataCiteToROCrateSchema] = DataCiteToROCrateSchema,
        include_exports: Mapping[str, str] = BAGIT_INCLUDED_EXPORTS,
        display: bool = True,
        display_if: Callable[[flask_principal.Identity | None], bool] | None = has_aip_access,
        **kwargs: Any,
    ):
        """Create the customization; the arguments are the same as for `AddROCrateBagItExport`.

        `display_if` defaults to `has_aip_access`, which lists the export only for holders of `aip-access`;
        pass `display_if=None` to list it for everyone or `display=False` to hide it from the landing page
        for everyone.
        """
        super().__init__(
            code=code,
            name=name,
            mimetype=mimetype,
            serializer=ROCrateAIPSerializer(
                schema_cls=schema_cls, include_exports=include_exports, extension=extension
            ),
            extension=extension,
            display=display,
            display_if=display_if,
            streaming=True,
            **kwargs,
        )


class AddROCrateImport(Customization):
    """Add the RO-Crate import to the model: `POST <url_prefix>/import/rocrate`.

    The route creates a record (a draft, unless the model has no drafts) from a posted RO-Crate - a detached
    crate whose files are fetched from their URLs, or an attached package (a ZIP or a BagIt bag, in the body
    or behind a URL) whose files are written from the package. The metadata is converted to DataCite JSON and
    handed to a data import of the model, **which the deployment writes** - the same way the exports require
    a DataCite export; with `?metadata=record-json` it is the package's own `metadata/record.json` instead,
    handed to the model's `json` import.

    ```python
    customizations = [
        AddMetadataImport(code="datacite", mimetype="application/vnd.datacite.datacite+json", ...),
        AddROCrateImport(),                      # uses the DataCite import above
        # ... the exports, as before
    ]
    ```

    A model without a DataCite import - or one that wants a lossless mapping of its own - converts the crate
    straight to its record structure and hands that to the identity import (see the import section of
    `docs/rocrate.md` for the schema):

    ```python
    AddROCrateImport(
        schema_cls=CrateToRecordSchema,
        base_import=IDENTITY_IMPORT,
    )
    ```

    The record may be published in the same request with `?publish`. When its files arrive after the
    response (fetched, or written from a package behind a URL), the publish is deferred to the task that
    follows them, which also mails the uploader (unless `?notifications=off`).
    See [oarepo_formats.rocrate.imports][], [oarepo_formats.rocrate.imports.tasks][] and
    [oarepo_formats.rocrate.imports.notifications][].
    """

    def __init__(
        self,
        *,
        base_import: str = DATACITE_JSON_MIMETYPE,
        deserializer: Any = None,
        schema_cls: type[Schema] = ROCrateToDataCiteSchema,
    ):
        """Create the customization.

        Args:
            base_import: mimetype (or code) of the model's import the converted metadata is handed to - the
                DataCite import by default, or `IDENTITY_IMPORT` (`"application/json"`, the model's `json`
                import, which passes the record JSON on unchanged) for a `schema_cls` that produces the
                record's own structure.
            deserializer: instead of the model's import, a deserializer the converted metadata is handed
                to - an escape hatch for a model without the import.
            schema_cls: the conversion of the crate's root data entity - `ROCrateToDataCiteSchema` or a
                subclass of it (to DataCite), or any marshmallow schema that dumps the record's own
                structure (with `base_import=IDENTITY_IMPORT`).

        """
        super().__init__("AddROCrateImport")
        self.options: dict[str, Any] = {
            "base_import": base_import,
            "deserializer": deserializer,
            "schema_cls": schema_cls,
        }

    @property
    def modifies(self) -> tuple[str, ...]:
        """The generated resource is the partial this customization changes."""
        return ("RecordResource",)

    def apply(self, builder: InvenioModelBuilder, model: InvenioModel) -> None:
        """Mix the route, with this model's config, into the model's resource.

        The config and the mixin class are created here, per model: one customization instance may be
        reused for several models (a deployment's `COMMON = [AddROCrateImport()]`), and a config built in
        `__init__` would then point to whichever model was built last.
        """
        config = ROCrateImportConfig(**self.options, model_name=model.base_name)
        mixin = type("ROCrateImportResourceMixin", (ROCrateImportResourceMixin,), {"rocrate_import": config})
        PrependMixin("RecordResource", mixin).apply(builder, model)
