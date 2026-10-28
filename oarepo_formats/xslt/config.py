# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Default configuration of the XSLT format; values in `invenio.cfg` win."""

from __future__ import annotations

OAREPO_XSLT_STYLESHEETS: dict[str, str] = {}
"""Stylesheet name -> location (`module:path` or `https:` URL).

Overrides the default location given in code, see [oarepo_formats.xslt.sources][].
"""

OAREPO_XSLT_PARAMS: dict[str, dict[str, str | bool]] = {}
"""Stylesheet name -> stylesheet parameters."""

OAREPO_XSLT_REFRESH_INTERVAL = 3600
"""Seconds after which a downloaded (`https:`) stylesheet is checked for a new version."""

OAREPO_XSLT_CHECK_INTERVAL = 60
"""Seconds for which a process uses its in-memory copy without consulting the shared cache."""

OAREPO_XSLT_ALLOWED_URL_PREFIXES: list[str] | None = None
"""If set, `https:` stylesheets must start with one of these prefixes."""

OAREPO_XSLT_MAX_SIZE = 5 * 1024 * 1024
"""Maximum size of a downloaded stylesheet in bytes."""

OAREPO_XSLT_DOWNLOAD_TIMEOUT = 10
"""Timeout of a stylesheet download in seconds."""

OAREPO_XSLT_TIMEOUT = 30
"""Maximum duration of one transformation in seconds; the sandbox worker is killed after that."""

OAREPO_XSLT_SANDBOX_WORKERS = 1
"""Number of sandbox worker processes per web/celery process."""

OAREPO_XSLT_MAX_MEMORY: int | None = None
"""Maximum data segment (RLIMIT_DATA) of a sandbox worker in bytes, Linux only."""
