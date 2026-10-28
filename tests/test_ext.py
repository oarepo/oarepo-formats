# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

from __future__ import annotations

from flask import Flask

from oarepo_formats.ext import OARepoFormats


def test_defaults_do_not_override_host_config():
    app = Flask("test")
    app.config["OAREPO_XSLT_TIMEOUT"] = 5
    OARepoFormats(app)
    assert app.config["OAREPO_XSLT_TIMEOUT"] == 5
    assert app.config["OAREPO_XSLT_SANDBOX_WORKERS"] == 1
