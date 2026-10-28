# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""A named XSLT stylesheet run in the sandbox, and the registry of the named stylesheets."""

from __future__ import annotations

import logging

from flask import current_app
from lxml import etree
from marshmallow import ValidationError
from oarepo_runtime.resources.xml import parse_xml

from ..proxies import current_formats
from .pool import SandboxError
from .sources import StylesheetError, load_stylesheet

log = logging.getLogger(__name__)

type ParamValue = str | bool

HARVESTER_PREFIX = "harvester:"
"""Name prefix of the transforms created by the harvester; the rest of the name is their location."""

transforms: dict[str, XSLTTransform] = {}
"""Transforms created in code, by name (used by the `invenio oarepo xslt` commands)."""


class XSLTTransform:
    """A named XSLT 3.0 stylesheet run by SaxonC-HE in the sandbox. Input and output are lxml elements.

    The stylesheet is loaded from `OAREPO_XSLT_STYLESHEETS[name]` if configured, otherwise from `default`
    (see [oarepo_formats.xslt.sources][] for the supported locations). Parameters are `params`, overridden
    by `OAREPO_XSLT_PARAMS[name]` and by per-call keyword arguments. `bool` values are passed as
    `xs:boolean`, everything else as `xs:string`.

    Nothing is loaded before the first call, so instances can be created at import time. All errors
    (invalid stylesheet, dynamic error, `xsl:message terminate="yes"`, sandbox timeout or crash, empty
    result) are raised as `marshmallow.ValidationError`; its messages are the error followed by the
    `xsl:message` output.

    ```python
    transform = XSLTTransform(
        "ccmm-1.1.0-to-2.0.0",
        "ccmm_invenio.xslt:ccmm-1.1.0-to-2.0.0.xslt",
    )
    tree = transform(element, flag=True)
    ```
    """

    def __init__(self, name: str, default: str, params: dict[str, ParamValue] | None = None):
        """Create the transform and register it under its name.

        Args:
            name: name used in `OAREPO_XSLT_STYLESHEETS`, `OAREPO_XSLT_PARAMS` and the CLI.
            default: location of the stylesheet if `OAREPO_XSLT_STYLESHEETS` does not configure the name.
            params: default stylesheet parameters.

        Raises:
            ValueError: if another transform with the same name has a different default location.

        """
        self.name = name
        self.default = default
        self.params = params or {}
        registered = transforms.setdefault(name, self)
        if registered.default != default:
            raise ValueError(f"XSLT {name} is already defined with location {registered.default}, not {default}")

    @property
    def location(self) -> str:
        """Effective location of the stylesheet."""
        return current_app.config["OAREPO_XSLT_STYLESHEETS"].get(self.name, self.default)

    def __call__(self, doc: etree._Element | etree._ElementTree, /, **params: ParamValue) -> etree._ElementTree:
        """Transform the document.

        Raises:
            marshmallow.ValidationError: on any error, see the class documentation.

        """
        if isinstance(doc, etree._ElementTree):  # noqa: SLF001
            doc = doc.getroot()
        try:
            stylesheet = load_stylesheet(self.location)
        except StylesheetError as e:
            raise ValidationError([f"XSLT {self.name}: {e}"]) from e
        request = {
            "stylesheet": stylesheet.text,
            # re-serialized from the parsed tree: no DOCTYPE, no entities reach Saxon
            "xml": etree.tostring(doc, encoding=str, with_tail=False),
            "params": {
                **self.params,
                **current_app.config["OAREPO_XSLT_PARAMS"].get(self.name, {}),
                **params,
            },
        }
        try:
            response = current_formats.xslt_pool.call(request)
        except SandboxError as e:
            raise ValidationError([f"XSLT {self.name}: {e}"]) from e

        messages = response.get("messages") or []
        for message in messages:
            log.debug("XSLT %s: xsl:message %s", self.name, message)
        if not response["ok"]:
            raise ValidationError([f"XSLT {self.name}: {response['error']}", *messages])
        if response["result_documents"]:
            log.warning("XSLT %s: discarded secondary result documents %s", self.name, response["result_documents"])
        try:
            return etree.ElementTree(parse_xml(response["xml"] or ""))
        except ValidationError as e:
            raise ValidationError([f"XSLT {self.name} did not produce an XML document"]) from e


def known_names() -> list[str]:
    """Names of the transforms created in code and of the stylesheets configured in `OAREPO_XSLT_STYLESHEETS`."""
    return sorted({*transforms, *current_app.config["OAREPO_XSLT_STYLESHEETS"]})


def get_transform(name: str) -> XSLTTransform:
    """Return the transform of the name.

    Harvester stylesheets (`harvester:<location>`) are found even if no harvest ran in this process.

    Raises:
        KeyError: if the name is not known.

    """
    if name in transforms:
        return transforms[name]
    if name in current_app.config["OAREPO_XSLT_STYLESHEETS"]:
        location = current_app.config["OAREPO_XSLT_STYLESHEETS"][name]
    elif name.startswith(HARVESTER_PREFIX):
        location = name.removeprefix(HARVESTER_PREFIX)
    else:
        raise KeyError(name)
    transform = XSLTTransform(name, location)
    del transforms[name]  # a lookup must not claim the name for a transform defined in code later
    return transform
