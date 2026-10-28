# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Format conversions for imports and exports of OARepo repositories.

Each format lives in its own subpackage, whose documentation is the reference for that format:

| Format | Package | |
|---|---|---|
| XSLT 3.0 | [oarepo_formats.xslt][] | sandboxed XSLT transformations of imported and exported XML |
| RO-Crate | [oarepo_formats.rocrate][] | import of a detached crate, export as a crate, ZIP or BagIt bag |
| README | [oarepo_formats.readme][] | export of a record as a `README.md`, rendered by an overridable Jinja template |

A format does not import another one. What they share lives here, at the package level: the Flask
extension [OARepoFormats][oarepo_formats.ext.OARepoFormats] (it merges the default configuration of every
format and holds per-process state) and its proxy
[current_formats][oarepo_formats.proxies.current_formats], the DataCite JSON contract the RO-Crate and
README formats are converted from ([oarepo_formats.datacite][] - its mimetype and the identifier helpers),
and the record's files as RO-Crate data entities with their checksums ([oarepo_formats.files][]).

## Adding a format

Create a subpackage `oarepo_formats.<format>` with the reference documentation in its `__init__.py`
and a page in `docs/`. If the format has configuration, put the defaults in `<format>/config.py`
(upper-case names, prefixed `OAREPO_<FORMAT>_`) and add the module to
[FORMAT_CONFIGS][oarepo_formats.ext.FORMAT_CONFIGS]; CLI commands go to `<format>/cli.py`, registered
under the `oarepo.cli` entry point.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("oarepo-formats")
except PackageNotFoundError:
    __version__ = "0.0.0dev0+unknown"

__all__ = ("__version__",)
