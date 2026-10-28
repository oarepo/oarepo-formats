# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

from __future__ import annotations

from importlib.metadata import entry_points

import pytest

from oarepo_formats.xslt import XSLTTransform
from oarepo_formats.xslt.cli import xslt
from tests.xslt.conftest import xsl

V1 = xsl(
    '<xsl:param name="flag" as="xs:boolean" select="false()"/><xsl:param name="label" as="xs:string" select="\'\'"/>'
    '<xsl:template match="/*"><out flag="{$flag}">{$label}</out></xsl:template>'
)


@pytest.fixture
def runner(app):
    return app.test_cli_runner()


@pytest.fixture
def configured(app, monkeypatch, stylesheet_file):
    """One stylesheet defined in code, one only in invenio.cfg."""
    code = XSLTTransform("from-code", stylesheet_file(V1))
    monkeypatch.setitem(app.config, "OAREPO_XSLT_STYLESHEETS", {"from-config": "tests.xslt.data:identity.xslt"})
    return code


def test_registered_in_oarepo_group():
    """`invenio oarepo xslt` is added by oarepo-runtime's `oarepo` group from this entry point."""
    assert [ep.load() for ep in entry_points(group="oarepo.cli", name="xslt")] == [xslt]


def test_list(runner, configured):
    result = runner.invoke(xslt, ["list"])
    assert result.exit_code == 0, result.output
    lines = sorted(result.output.splitlines())
    assert lines[0].startswith("from-code\txslt_stylesheets_")
    assert lines[1].startswith("from-config\ttests.xslt.data:identity.xslt\t")


def test_show(runner, configured):
    result = runner.invoke(xslt, ["show", "from-code"])
    assert result.output.strip() == V1


def test_unknown_name(runner, configured):
    result = runner.invoke(xslt, ["show", "nope"])
    assert result.exit_code != 0
    assert "Unknown stylesheet nope, known are: from-code, from-config" in result.output


def test_refresh(runner, configured, app, monkeypatch, stylesheet_file):
    result = runner.invoke(xslt, ["refresh"])
    assert result.exit_code == 0, result.output
    assert "from-code" in result.output
    assert "from-config" in result.output
    # a stylesheet that does not compile fails the command (and the deploy)
    broken = stylesheet_file(xsl('<xsl:template match="/"><xsl:value-of select="1 +"/></xsl:template>'))
    monkeypatch.setitem(app.config, "OAREPO_XSLT_STYLESHEETS", {"from-config": broken})
    result = runner.invoke(xslt, ["refresh", "from-config"])
    assert result.exit_code == 1
    assert "from-config" in result.output
    assert "Cannot compile stylesheet" in result.output


def test_test(runner, configured, tmp_path):
    input_file = tmp_path / "in.xml"
    input_file.write_text("<r/>")
    result = runner.invoke(xslt, ["test", "from-code", str(input_file), "-p", "flag=true", "-p", "label=hi"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == '<out flag="true">hi</out>'
