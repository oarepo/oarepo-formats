# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of converting the detached RO-Crate document to an attached one."""

from __future__ import annotations

import pytest

from oarepo_formats.files import file_entities
from oarepo_formats.rocrate.exports.graph import flatten
from oarepo_formats.rocrate.exports.zip import CrateContent, attach

FILES_URL = "https://repo/api/r/1/files"


def document(*keys: str, root_id: str = "https://doi.org/10.1/x", cite_as: str | None = "https://doi.org/10.1/x"):
    """Return a metadata export document with the given file keys."""
    record = {"links": {"files": FILES_URL}, "files": {"entries": {key: {"size": 1} for key in keys}}}
    tree = {"@id": root_id, "@type": "Dataset", "hasPart": file_entities(record)}
    if cite_as:
        tree["cite-as"] = cite_as
    return flatten(tree)


def by_id(doc: dict) -> dict[str, dict]:
    return {e["@id"]: e for e in doc["@graph"]}


def crate_of(doc: dict) -> CrateContent:
    return CrateContent(document=doc, members=[], files=[], record=None)


def test_landing_page_root_is_kept_as_url():
    crate = crate_of(document(root_id="https://repo/r/1", cite_as=None))
    attach(crate)

    assert by_id(crate.document)["./"]["url"] == "https://repo/r/1"


@pytest.mark.parametrize("key", ["ro-crate-metadata.json", "README.generated.md", "metadata/datacite.xml"])
def test_files_are_under_data(key):
    """Record files can not clash with the members the archive adds: they are all under data/."""
    crate = crate_of(document(key))
    paths = attach(crate)

    assert paths == {key: f"data/{key}"}
    assert by_id(crate.document)["./"]["hasPart"] == [{"@id": f"data/{key}"}]


@pytest.mark.parametrize("key", ["../evil.txt", "/abs.txt", "a/../../b.txt", "a//b.txt", "a/./b.txt"])
def test_unsafe_key_is_rejected(key):
    with pytest.raises(ValueError, match="can not be used as a path"):
        attach(crate_of(document(key)))
