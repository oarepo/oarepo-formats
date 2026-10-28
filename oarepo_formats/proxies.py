# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Proxies."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from flask import current_app
from werkzeug.local import LocalProxy

if TYPE_CHECKING:
    from oarepo_formats.ext import OARepoFormats

current_formats = cast("OARepoFormats", LocalProxy(lambda: current_app.extensions["oarepo-formats"]))
"""The [OARepoFormats][oarepo_formats.ext.OARepoFormats] extension of the current application."""
