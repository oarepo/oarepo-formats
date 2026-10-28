# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""oarepo-model customizations registering an XSLT export or import built on another one.

They take the arguments of oarepo-model's `AddMetadataExport` / `AddMetadataImport` plus the stylesheet.
The export applies it to the output of another export of the model (`base_export`), the import to
the request body before handing it to another import of the model (`base_import`). A serializer or
deserializer can be given instead. See [oarepo_formats.xslt][] for examples and where stylesheets come
from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

from flask_resources.deserializers import DeserializerMixin
from flask_resources.serializers import BaseSerializer
from lxml import etree
from oarepo_model.customizations import AddMetadataExport, AddMetadataImport
from oarepo_runtime.api import ExportEngine, ExportRepresentation
from oarepo_runtime.proxies import current_runtime
from oarepo_runtime.resources.xml import XMLDeserializer

from .serializers import XSLTDeserializer, XSLTSerializer
from .transform import ParamValue, XSLTTransform

if TYPE_CHECKING:
    from flask_babel.speaklater import LazyString
    from oarepo_model.builder import InvenioModelBuilder
    from oarepo_model.model import InvenioModel


class AddXSLTExport(AddMetadataExport):
    """Add an export that applies a stylesheet to the output of another XML export of the model.

    ```python
    customizations = [
        AddMetadataExport(code="ccmm-xml", ..., serializer=CCMMXMLSerializer()),
        AddXSLTExport(
            code="ccmm-xml-2",
            name=_("CCMM 2.0 XML"),
            mimetype="application/x-ccmm-2+xml",
            stylesheet="ccmm_invenio.xslt:ccmm-1.1.0-to-2.0.0.xslt",
            base_export="ccmm-xml",
        ),
    ]
    ```

    If the source format is not an export of the model, pass `serializer=CCMMXMLSerializer()` instead
    of `base_export`.
    """

    def __init__(  # noqa: PLR0913 - the export options plus the stylesheet
        self,
        *,
        code: str,
        name: LazyString,
        mimetype: str,
        stylesheet: str,
        base_export: str | None = None,
        serializer: BaseSerializer | None = None,
        stylesheet_name: str | None = None,
        params: dict[str, ParamValue] | None = None,
        extension: str = ".xml",
        **kwargs: Any,
    ):
        """Create the customization.

        Args:
            code: code of the export.
            name: human-readable name of the export, shown on the landing page.
            mimetype: mimetype of the export, used for content negotiation (`Accept` header).
            stylesheet: default location of the stylesheet, see [oarepo_formats.xslt.sources][].
            base_export: code of the XML export of the model whose output the stylesheet transforms.
                It is looked up when a record is exported and shares the export cache with this export;
                it may be an XSLT export as well.
            serializer: instead of `base_export`, an XML serializer whose output the stylesheet
                transforms.
            stylesheet_name: name of the stylesheet in `OAREPO_XSLT_STYLESHEETS`, `OAREPO_XSLT_PARAMS`
                and the `invenio oarepo xslt` commands; `export:<code>` by default.
            params: default stylesheet parameters.
            extension: file extension of the export.
            **kwargs: any other `AddMetadataExport` / `Export` option, e.g. `display` or the `oai_*` options.

        Raises:
            TypeError: unless exactly one of `base_export` and `serializer` is given.

        """
        if base_export is not None:
            if serializer is not None:
                raise TypeError("AddXSLTExport takes base_export or serializer, not both")
            serializer = _ExportByCode(base_export)
        elif serializer is None:
            raise TypeError("AddXSLTExport needs base_export or serializer")
        transform = XSLTTransform(stylesheet_name or f"export:{code}", stylesheet, params)
        super().__init__(
            code=code,
            name=name,
            mimetype=mimetype,
            serializer=XSLTSerializer(transform, serializer),
            extension=extension,
            **kwargs,
        )


class AddXSLTImport(AddMetadataImport):
    """Add an import that applies a stylesheet to the request body and hands it to another XML import of the model.

    ```python
    customizations = [
        AddMetadataImport(code="ccmm-xml", ..., deserializer=CCMMXMLDeserializer()),
        AddXSLTImport(
            code="ccmm-xml-2",
            name=_("CCMM 2.0 XML"),
            description=_("CCMM 2.0 XML document"),
            mimetype="application/x-ccmm-2+xml",
            stylesheet="ccmm_invenio.xslt:ccmm-2.0.0-to-1.1.0.xslt",
            base_import="ccmm-xml",
        ),
    ]
    ```

    If the target format is not an import of the model, pass `deserializer=CCMMXMLDeserializer()`
    instead of `base_import`.
    """

    def __init__(  # noqa: PLR0913 - the import options plus the stylesheet
        self,
        *,
        code: str,
        name: LazyString,
        description: LazyString,
        mimetype: str,
        stylesheet: str,
        base_import: str | None = None,
        deserializer: DeserializerMixin | None = None,
        stylesheet_name: str | None = None,
        params: dict[str, ParamValue] | None = None,
        **kwargs: Any,
    ):
        """Create the customization.

        Args:
            code: code of the import.
            name: human-readable name of the import.
            description: human-readable description of the import.
            mimetype: mimetype of the import, selected by the `Content-Type` of the request.
            stylesheet: default location of the stylesheet, see [oarepo_formats.xslt.sources][].
            base_import: code of the XML import of the model the transformed body is handed to. It is
                looked up on the first request, so it may be declared after this import; it may be an
                XSLT import as well.
            deserializer: instead of `base_import`, a deserializer the transformed body is handed to.
            stylesheet_name: name of the stylesheet in `OAREPO_XSLT_STYLESHEETS`, `OAREPO_XSLT_PARAMS`
                and the `invenio oarepo xslt` commands; `import:<code>` by default.
            params: default stylesheet parameters.
            **kwargs: any other `AddMetadataImport` option, e.g. `oai_name`.

        Raises:
            TypeError: unless exactly one of `base_import` and `deserializer` is given.

        """
        self._base_import: str | None = None
        if base_import is not None:
            if deserializer is not None:
                raise TypeError("AddXSLTImport takes base_import or deserializer, not both")
            self._base_import = base_import
            # a placeholder: `apply` replaces it with a lookup bound to the model being built
            deserializer = _ImportByCode(base_import, model_name="")
        elif deserializer is None:
            raise TypeError("AddXSLTImport needs base_import or deserializer")
        transform = self._transform = XSLTTransform(stylesheet_name or f"import:{code}", stylesheet, params)
        super().__init__(
            code=code,
            name=name,
            description=description,
            mimetype=mimetype,
            deserializer=XSLTDeserializer(transform, deserializer),
            **kwargs,
        )

    @override
    def apply(self, builder: InvenioModelBuilder, model: InvenioModel) -> None:
        """Register the import, with a lookup bound to *this* model.

        The lookup is created here, per model: one `AddXSLTImport` instance may be reused for several models
        (a deployment's `COMMON = [AddXSLTImport(...)]`), and a lookup built in `__init__` would point to
        whichever model was built last.
        """
        if self._base_import is not None:
            lookup = _ImportByCode(self._base_import, model_name=model.base_name)
            self._options["deserializer"] = XSLTDeserializer(self._transform, lookup)
        super().apply(builder, model)


class _ExportByCode(BaseSerializer):
    """XML of another export of the record's model."""

    def __init__(self, code: str):
        self.code = code

    def serialize_object_xml(self, obj: dict) -> etree._Element:
        return ExportEngine.export(obj, export_code=self.code, representation=ExportRepresentation.XML)

    def serialize_object(self, obj: dict) -> str:
        return etree.tostring(self.serialize_object_xml(obj), encoding=str)


class _ImportByCode(DeserializerMixin):
    """Another import of the model, looked up in the built model on every request."""

    def __init__(self, code: str, model_name: str):
        self.code = code
        self.model_name = model_name

    def deserialize_xml(self, element: etree._Element) -> dict[str, Any]:
        found = current_runtime.models[self.model_name].get_import(self.code)
        if found is None:
            raise ValueError(f"The model has no import {self.code!r} (base_import of an XSLT import)")
        if isinstance(found.deserializer, XMLDeserializer):
            return found.deserializer.deserialize_xml(element)
        return found.deserializer.deserialize(etree.tostring(element, encoding=str))
