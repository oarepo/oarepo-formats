# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of the BagIt path encoding and declared sizes - input the REST API tests do not produce.

The rest of the tag files (line format, sorting, `bag-info.txt`, the bag directory) is checked end to
end by bagit-python in `tests/api_tests/test_rocrate_bagit.py`.
"""

from __future__ import annotations

import pytest

from oarepo_formats.rocrate.exports.bag import bag_info, bag_name, manifest, manifest_path, manifest_size


@pytest.mark.parametrize(
    ("crate_path", "expected"),
    [
        ("data/my file.csv", "data/data/my file.csv"),  # a space stays literal (RFC 8493 2.1.3)
        ("data/ž.txt", "data/data/ž.txt"),  # non-ASCII stays literal, the tag files are UTF-8
        ("data/100%.txt", "data/data/100%25.txt"),  # percent is encoded
        ("data/a\rb.txt", "data/data/a%0Db.txt"),
        ("data/a\nb.txt", "data/data/a%0Ab.txt"),
    ],
)
def test_manifest_path(crate_path, expected):
    assert manifest_path(crate_path) == expected


@pytest.mark.parametrize("crate_path", ["data/a.txt", "data/my file ž.txt", "data/100%.txt"])
def test_declared_size_matches_the_generated_manifest(crate_path):
    """A wrong declared size corrupts the archive after the headers were sent."""
    path = manifest_path(crate_path)

    assert manifest_size([path], 32) == len(manifest({path: "0" * 32}))
    assert manifest_size([path], 128) == len(manifest({path: "0" * 128}))


@pytest.mark.parametrize("record_id", ["", ".", "..", "a/b", "a b", "../x", "ž"])
def test_bag_name_refuses_an_unsafe_id(record_id):
    """The bag directory has to be one path segment: `..` would escape it on extraction."""
    with pytest.raises(ValueError, match="directory of the bag"):
        bag_name(record_id)


def test_bag_info_keeps_a_value_on_one_line():
    """A newline in a value (a title) may not break the tag file into a continuation line."""
    assert bag_info([("External-Description", "line one\nline two")]) == (b"External-Description: line one line two\n")
