# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Stylesheets are third-party code: each attack that works in a default Saxon must fail here."""

from __future__ import annotations

import http.server
import os
import threading
from typing import TYPE_CHECKING

import pytest
from lxml import etree
from marshmallow import ValidationError

from oarepo_formats.proxies import current_formats
from oarepo_formats.xslt import XSLTTransform
from tests.xslt.conftest import xsl

if TYPE_CHECKING:
    from pathlib import Path

SECRET = "oarepo-formats-secret-marker"  # noqa: S105 - not a password


def run(stylesheet_file, body: str) -> str:
    """Run the stylesheet on <r/> and return the serialized result."""
    location = stylesheet_file(xsl(body))
    transform = XSLTTransform(location, location)
    return etree.tostring(transform(etree.fromstring("<r/>")), encoding=str)


@pytest.fixture
def secret_env(monkeypatch):
    monkeypatch.setenv("OAREPO_FORMATS_SECRET", SECRET)


@pytest.fixture
def local_file(tmp_path: Path) -> Path:
    path = tmp_path / "local.xml"
    path.write_text(f"<secret>{SECRET}</secret>")
    return path


@pytest.fixture
def http_hits():
    """Local HTTP server that records every request it gets."""
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<x/>")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/", hits
    server.shutdown()


def reading(expression: str):
    """Return a stylesheet body that reads the local file through `expression`."""
    return lambda uri, stylesheet_file: (
        f'<xsl:template match="/"><out><xsl:value-of select="{expression.format(uri=uri)}"/></out></xsl:template>'
    )


def including(uri, stylesheet_file):
    """Return a stylesheet body that includes the local file: a compile-time URI access."""
    return f'<xsl:include href="{uri}"/>'


@pytest.mark.parametrize(
    "stylesheet",
    [
        pytest.param(reading("unparsed-text('{uri}')"), id="unparsed-text"),
        pytest.param(reading("string(doc('{uri}'))"), id="doc"),
        pytest.param(
            reading("function-lookup(QName('http://www.w3.org/2005/xpath-functions', 'unparsed-text'), 1)('{uri}')"),
            id="function-lookup",
        ),
        pytest.param(
            reading("string(transform(map{{'stylesheet-location': '{uri}', 'source-node': .}})?output)"),
            id="transform",
        ),
        # stylesheets must be self-contained
        pytest.param(including, id="include"),
    ],
)
def test_local_files_cannot_be_reached(stylesheet_file, local_file, stylesheet):
    """Saxon reads local files by default; allowedProtocols="" must block every way to do it."""
    with pytest.raises(ValidationError, match="has been prohibited"):
        run(stylesheet_file, stylesheet(local_file.as_uri(), stylesheet_file))


def test_network_is_not_reachable(stylesheet_file, http_hits):
    url, hits = http_hits
    with pytest.raises(ValidationError, match="has been prohibited"):
        run(
            stylesheet_file,
            f'<xsl:template match="/"><out><xsl:copy-of select="doc(\'{url}\')"/></out></xsl:template>',
        )
    assert hits == []


def test_result_document_is_not_written(stylesheet_file, tmp_path, caplog):
    target = tmp_path / "written.xml"
    body = (
        f'<xsl:template match="/"><xsl:result-document href="{target.as_uri()}"><x/></xsl:result-document>'
        "<out/></xsl:template>"
    )
    assert run(stylesheet_file, body) == "<out/>"
    assert not target.exists()
    assert "discarded secondary result documents" in caplog.text


@pytest.mark.parametrize(
    "expression",
    [
        "environment-variable('OAREPO_FORMATS_SECRET')",
        "string-join(available-environment-variables() ! environment-variable(.), ' ')",
        (
            "function-lookup(QName('http://www.w3.org/2005/xpath-functions', 'environment-variable'), 1)"
            "('OAREPO_FORMATS_SECRET')"
        ),
    ],
)
def test_environment_is_not_visible(stylesheet_file, secret_env, expression):
    body = f'<xsl:template match="/"><out><xsl:value-of select="{expression}"/></out></xsl:template>'
    output = run(stylesheet_file, body)
    assert SECRET not in output


def test_environment_is_not_visible_via_evaluate(stylesheet_file, secret_env):
    """xsl:evaluate builds the function call at runtime, so a static scan could not catch it."""
    body = (
        '<xsl:template match="/"><out>'
        "<xsl:evaluate xpath=\"'environment-variable(''OAREPO_FORMATS_' || 'SECRET'')'\"/>"
        "|<xsl:evaluate xpath=\"'1 + 1'\"/></out></xsl:template>"
    )
    # the second xsl:evaluate proves that dynamic evaluation itself runs in Saxon-HE
    assert run(stylesheet_file, body) == "<out>|2</out>"


def test_infinite_recursion_is_killed(app, monkeypatch, stylesheet_file):
    monkeypatch.setattr(current_formats.xslt_pool, "timeout", 2)
    body = (
        '<xsl:function name="f:loop" as="xs:integer"><xsl:param name="n" as="xs:integer"/>'
        '<xsl:sequence select="f:loop($n + 1)"/></xsl:function>'
        '<xsl:template match="/"><out><xsl:value-of select="f:loop(1)"/></out></xsl:template>'
    )
    with pytest.raises(ValidationError, match="timed out"):
        run(stylesheet_file, body)
    # the killed worker is replaced on the next call
    assert run(stylesheet_file, '<xsl:template match="/"><ok/></xsl:template>') == "<ok/>"


def test_fork_gets_its_own_pool(stylesheet_file):
    """A pre-forking server forks after workers started; the child must not share their pipes."""
    ok = '<xsl:template match="/"><ok/></xsl:template>'
    run(stylesheet_file, ok)
    parent_pool = current_formats.xslt_pool
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        try:
            result = run(stylesheet_file, ok)
            os.write(write_fd, f"{current_formats.xslt_pool is not parent_pool}:{result}".encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    os.waitpid(pid, 0)
    with os.fdopen(read_fd) as f:
        assert f.read() == "True:<ok/>"
    assert current_formats.xslt_pool is parent_pool
