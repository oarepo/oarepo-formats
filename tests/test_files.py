# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of record files as data entities."""

from __future__ import annotations

from oarepo_formats.files import file_entities


def test_file_entities():
    """Only files the user may download are listed: hidden and not yet committed files are skipped."""
    record = {
        "links": {"files": "https://repo/api/records/abc/files"},
        "files": {
            "entries": {
                "dir/my data.csv": {
                    "size": 26452,
                    "mimetype": "text/csv",
                    "status": "completed",
                    "checksum": "md5:0123456789abcdef0123456789abcdef",
                },
                "hidden.txt": {"size": 1, "access": {"hidden": True}},
                "pending.txt": {"status": "pending"},
            }
        },
    }

    (entity,) = file_entities(record)

    assert entity.pop("sdDatePublished").endswith("+00:00")
    assert entity == {
        "@id": "https://repo/api/records/abc/files/dir/my%20data.csv/content",
        "@type": "File",
        "name": "dir/my data.csv",
        "localPath": "dir/my data.csv",
        "contentSize": "26452",
        "encodingFormat": "text/csv",
        "md5": "0123456789abcdef0123456789abcdef",  # Invenio's stored checksum, "md5:<hex>"
    }
