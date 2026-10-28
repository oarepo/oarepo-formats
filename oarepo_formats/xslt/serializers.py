# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""(De)serializers that apply an XSLT before an import / after an export.

Both wrap an existing (de)serializer and implement the element-level protocols of
`oarepo_runtime.resources.xml` (`deserialize_xml`, `serialize_object_xml`), so the OAI-PMH harvester
and server do not serialize and parse the document again. If the wrapped (de)serializer implements the
protocol as well, the XML is passed to it as an element; otherwise as a string.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from flask_resources.deserializers import DeserializerMixin
from flask_resources.serializers import BaseSerializer
from lxml import etree
from marshmallow import ValidationError
from oarepo_runtime.resources.xml import XMLDeserializer, XMLSerializer, parse_xml

if TYPE_CHECKING:
    from .transform import XSLTTransform

log = logging.getLogger(__name__)


class XSLTDeserializer(DeserializerMixin):
    """Applies an XSLT to the incoming XML, then hands the result to another deserializer.

    [AddXSLTImport][oarepo_formats.xslt.model.AddXSLTImport] creates it, so it is normally not used directly;
    `deserializer` is the deserializer given to it or the `base_import` of the model.
    """

    def __init__(self, transform: XSLTTransform, deserializer: DeserializerMixin):
        """Wrap the deserializer."""
        self.transform = transform
        self.deserializer = deserializer

    def deserialize(self, data: str | bytes) -> dict[str, Any]:
        """Deserialize a REST API body.

        Raises:
            marshmallow.ValidationError: if the body is not well-formed XML or the transformation fails.

        """
        return self.deserialize_xml(parse_xml(data))

    def deserialize_xml(self, element: etree._Element) -> dict[str, Any]:
        """Deserialize an already parsed element (OAI-PMH harvester).

        Raises:
            marshmallow.ValidationError: if the transformation fails.

        """
        result = self.transform(element).getroot()
        if isinstance(self.deserializer, XMLDeserializer):
            return self.deserializer.deserialize_xml(result)
        return self.deserializer.deserialize(etree.tostring(result, encoding=str))


class XSLTSerializer(BaseSerializer):
    """Serializes with another (XML) serializer, then applies an XSLT to its output.

    [AddXSLTExport][oarepo_formats.xslt.model.AddXSLTExport] creates it, so it is normally not used directly;
    `serializer` is the serializer given to it or the `base_export` of the model.
    Serializes single records only; there is no common XML envelope for a list of records.
    """

    def __init__(self, transform: XSLTTransform, serializer: BaseSerializer):
        """Wrap the serializer."""
        self.transform = transform
        self.serializer = serializer

    def serialize_object_xml(self, obj: Any) -> etree._Element:
        """Serialize the record into an XML element.

        Raises:
            marshmallow.ValidationError: if the transformation fails; the error is logged with the record id.

        """
        if isinstance(self.serializer, XMLSerializer):
            source = self.serializer.serialize_object_xml(obj)
        else:
            source = parse_xml(self.serializer.serialize_object(obj))
        try:
            return self.transform(source).getroot()
        except ValidationError as e:
            record_id = obj.get("id") if isinstance(obj, dict) else getattr(obj, "id", None)
            log.error("XSLT %s failed for record %s: %s", self.transform.name, record_id, e.messages)  # noqa: TRY400
            raise

    def serialize_object(self, obj: Any) -> str:
        """Serialize the record into an XML string."""
        return etree.tostring(self.serialize_object_xml(obj), encoding=str, xml_declaration=False)

    def serialize_object_list(self, obj_list: Any) -> Any:
        """Not supported.

        Delegating to the wrapped serializer would return the records untransformed, in the source format.

        Raises:
            NotImplementedError: always.

        """
        raise NotImplementedError(f"XSLT {self.transform.name} serializes single records only")
