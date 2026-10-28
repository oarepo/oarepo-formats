# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

from __future__ import annotations

import pytest
from lxml import etree
from marshmallow import ValidationError

from oarepo_formats.xslt import XSLTTransform
from tests.xslt.conftest import xsl

# maps and || would not compile in libxslt: proves the XSLT 3.0 engine is used
XSLT3 = xsl(
    '<xsl:param name="flag" as="xs:boolean" select="false()"/>'
    '<xsl:param name="label" as="xs:string" select="\'default\'"/>'
    '<xsl:template match="/*"><out flag="{$flag}">{map{"a": "x"}?a || $label}</out></xsl:template>'
)


def result(tree: etree._ElementTree) -> str:
    return etree.tostring(tree, encoding=str)


def test_xslt3_with_typed_params(stylesheet_file):
    """Bools must reach Saxon as xs:boolean: an xs:string would not match as="xs:boolean"."""
    transform = XSLTTransform("t", stylesheet_file(XSLT3), {"label": "-code"})
    doc = etree.fromstring("<r/>")
    assert result(transform(doc)) == '<out flag="false">x-code</out>'
    assert result(transform(doc, flag=True, label="-call")) == '<out flag="true">x-call</out>'


def test_same_name_other_location_is_refused():
    XSLTTransform("t", "tests.xslt.data:identity.xslt")
    with pytest.raises(ValueError, match="already defined"):
        XSLTTransform("t", "tests.xslt.data:other.xslt")


def test_params_from_config(app, monkeypatch, stylesheet_file):
    """Precedence: code < OAREPO_XSLT_PARAMS < per-call arguments."""
    monkeypatch.setitem(app.config, "OAREPO_XSLT_PARAMS", {"t": {"label": "-config", "flag": True}})
    transform = XSLTTransform("t", stylesheet_file(XSLT3), {"label": "-code"})
    doc = etree.fromstring("<r/>")
    assert result(transform(doc)) == '<out flag="true">x-config</out>'
    assert result(transform(doc, label="-call")) == '<out flag="true">x-call</out>'


def test_config_overrides_default_location(app, monkeypatch, stylesheet_file):
    other = stylesheet_file(xsl('<xsl:template match="/"><configured/></xsl:template>'))
    monkeypatch.setitem(app.config, "OAREPO_XSLT_STYLESHEETS", {"t": other})
    transform = XSLTTransform("t", stylesheet_file(XSLT3))
    assert result(transform(etree.fromstring("<r/>"))) == "<configured/>"


def body(text: str):
    """Return a stylesheet with this body, written to a file."""
    return lambda stylesheet_file: stylesheet_file(xsl(text))


def location(text: str):
    """Return a stylesheet location as it is."""
    return lambda stylesheet_file: text


@pytest.mark.parametrize(
    ("stylesheet", "error"),
    [
        pytest.param(location("tests.xslt.data:missing.xslt"), "Cannot read stylesheet", id="missing"),
        pytest.param(location("http://example.org/x.xslt"), "Unsupported stylesheet location", id="unsupported"),
        pytest.param(
            body('<xsl:template match="/"><xsl:value-of select="1 +"/></xsl:template>'),
            "Cannot compile stylesheet",
            id="does-not-compile",
        ),
        pytest.param(
            body('<xsl:template match="/"><xsl:value-of select="error()"/></xsl:template>'),
            "Error signalled by application",
            id="error-at-runtime",
        ),
        pytest.param(body('<xsl:template match="/"/>'), "did not produce an XML document", id="no-document"),
    ],
)
def test_a_failing_stylesheet_is_a_validation_error(stylesheet_file, stylesheet, error):
    """Every failure is a ValidationError, so REST returns 400 and the harvester records it."""
    with pytest.raises(ValidationError, match=error):
        XSLTTransform("t", stylesheet(stylesheet_file))(etree.fromstring("<r/>"))


def test_terminating_message(stylesheet_file):
    """xsl:message terminate="yes" is how the stylesheets reject a document of the wrong version."""
    body = (
        '<xsl:template match="/"><xsl:message>first</xsl:message>'
        '<xsl:message terminate="yes">wrong version</xsl:message></xsl:template>'
    )
    with pytest.raises(ValidationError) as e:
        XSLTTransform("t", stylesheet_file(xsl(body)))(etree.fromstring("<r/>"))
    assert e.value.messages[1:] == ["first", "wrong version"]
