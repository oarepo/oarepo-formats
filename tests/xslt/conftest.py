# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Pytest fixtures of the XSLT tests.

They need no services: a bare Flask app with a SimpleCache flask-caching backend and the real
sandbox worker (SaxonC-HE in a subprocess).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from flask import Flask
from invenio_cache import InvenioCache, current_cache

from oarepo_formats.ext import OARepoFormats
from oarepo_formats.xslt import transform
from oarepo_formats.xslt.sources import clear_memory_cache

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

XSL_HEADER = (
    '<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"'
    ' xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:f="urn:f" expand-text="yes" exclude-result-prefixes="#all">'
)


def xsl(body: str) -> str:
    """Wrap templates into an XSLT 3.0 stylesheet."""
    return f"{XSL_HEADER}{body}</xsl:stylesheet>"


@pytest.fixture(scope="session")
def app():
    """Minimal app; the sandbox pool lives for the whole session (worker startup is slow)."""
    app = Flask("oarepo-formats-tests")
    app.config.update(CACHE_TYPE="SimpleCache", OAREPO_XSLT_TIMEOUT=10)
    InvenioCache(app)
    ext = OARepoFormats(app)
    with app.app_context():
        yield app
    ext.xslt_pool.close()


@pytest.fixture(autouse=True)
def clean_state(app):
    """Each test starts with empty caches and no registered transforms."""
    yield
    clear_memory_cache()
    current_cache.clear()
    transform.transforms.clear()


@pytest.fixture
def stylesheet_file(tmp_path: Path, monkeypatch) -> Callable[[str], str]:
    """Write a stylesheet into a package of its own and return its ``module:path`` location."""
    # a new package name per test: python caches imported packages in sys.modules
    package = f"xslt_stylesheets_{uuid.uuid4().hex}"
    (tmp_path / package).mkdir()
    (tmp_path / package / "__init__.py").touch()
    monkeypatch.syspath_prepend(str(tmp_path))
    counter = iter(range(1000))

    def write(text: str) -> str:
        name = f"stylesheet-{next(counter)}.xslt"
        (tmp_path / package / name).write_text(text)
        return f"{package}:{name}"

    return write
