# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""OARepo formats extension.

Merges the default configuration of every format into the application config (values set in
`invenio.cfg` win) and holds per-process state of the formats. Code reaches it through
[current_formats][oarepo_formats.proxies.current_formats].
"""

from __future__ import annotations

import copy
import os
import threading
from typing import TYPE_CHECKING

from . import config as formats_config
from .rocrate import config as rocrate_config
from .xslt import config as xslt_config
from .xslt.pool import SandboxPool

if TYPE_CHECKING:
    from flask import Flask

FORMAT_CONFIGS = (formats_config, rocrate_config, xslt_config)
"""Configuration modules of the formats; their upper-case names are the defaults."""


class OARepoFormats:
    """Format conversions for imports and exports."""

    def __init__(self, app: Flask | None = None):
        """Extension initialization."""
        self._xslt_pool: SandboxPool | None = None
        self._xslt_pool_lock = threading.Lock()
        if app:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        """Flask application initialization."""
        self.app = app
        self.init_config(app)
        app.extensions["oarepo-formats"] = self

    def init_config(self, app: Flask) -> None:
        """Set the default configuration of all formats, register the harvester transformer and the mails."""
        from .xslt.harvester import register_harvester_transformer

        for module in FORMAT_CONFIGS:
            for key in dir(module):
                if key.isupper() and key not in app.config:
                    value = getattr(module, key)
                    if isinstance(value, (list, dict)):
                        value = copy.copy(value)
                    app.config[key] = getattr(module, key)
        from .rocrate.imports.notifications import BUILDERS

        # a new dict, never a mutation: oarepo's initial config assigns the RDM dict and must not be
        # changed in place; the deployment's own builders win over ours
        app.config["NOTIFICATIONS_BUILDERS"] = {**BUILDERS, **(app.config.get("NOTIFICATIONS_BUILDERS") or {})}
        register_harvester_transformer(app)

    @property
    def xslt_pool(self) -> SandboxPool:
        """XSLT sandbox worker pool of this process, recreated after a fork."""
        with self._xslt_pool_lock:
            if self._xslt_pool is None or self._xslt_pool.pid != os.getpid():
                # after a fork the workers belong to the parent; just forget them
                self._xslt_pool = SandboxPool(
                    size=self.app.config["OAREPO_XSLT_SANDBOX_WORKERS"],
                    timeout=self.app.config["OAREPO_XSLT_TIMEOUT"],
                    max_memory=self.app.config["OAREPO_XSLT_MAX_MEMORY"],
                )
            return self._xslt_pool
