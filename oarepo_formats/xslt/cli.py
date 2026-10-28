# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""`invenio oarepo xslt` commands.

| Command | |
|---|---|
| `invenio oarepo xslt list` | stylesheets with their location, sha256 and download time |
| `invenio oarepo xslt show NAME` | the effective text of a stylesheet |
| `invenio oarepo xslt refresh [NAME...]` | download (`https:`) and compile the stylesheets, all if no name is given |
| `invenio oarepo xslt test NAME FILE [-p key=value]` | run a stylesheet on a file in the sandbox and print the result |

The known names are those of the transforms created in code and those configured in
`OAREPO_XSLT_STYLESHEETS`; harvester stylesheets are addressed as `harvester:<location>`.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import click
from flask.cli import with_appcontext
from lxml import etree
from marshmallow import ValidationError
from oarepo_runtime.resources.xml import parse_xml

from .sources import StylesheetError, check_stylesheet, load_stylesheet
from .transform import XSLTTransform, get_transform, known_names


def _transform(name: str) -> XSLTTransform:
    try:
        return get_transform(name)
    except KeyError:
        raise click.ClickException(f"Unknown stylesheet {name}, known are: {', '.join(known_names())}") from None


@click.group()
def xslt() -> None:
    """XSLT stylesheets used by imports and exports."""


@xslt.command(name="list")
@with_appcontext
def list_stylesheets() -> None:
    """List stylesheets with their location, sha256 and download time."""
    for name in known_names():
        transform = _transform(name)
        try:
            stylesheet = load_stylesheet(transform.location)
        except StylesheetError as e:
            click.echo(f"{name}\t{transform.location}\tERROR: {e}")
            continue
        fetched_at = (
            dt.datetime.fromtimestamp(stylesheet.fetched_at, tz=dt.UTC).isoformat() if stylesheet.fetched_at else "-"
        )
        click.echo(f"{name}\t{transform.location}\t{stylesheet.sha256}\t{fetched_at}")


@xslt.command()
@click.argument("name")
@with_appcontext
def show(name: str) -> None:
    """Print the effective text of a stylesheet."""
    try:
        click.echo(load_stylesheet(_transform(name).location).text)
    except StylesheetError as e:
        raise click.ClickException(str(e)) from e


@xslt.command()
@click.argument("names", nargs=-1)
@with_appcontext
def refresh(names: tuple[str, ...]) -> None:
    """Download (https:) and compile stylesheets; all known ones if no NAME is given."""
    failed = False
    for name in names or known_names():
        location = _transform(name).location
        try:
            check_stylesheet(load_stylesheet(location, force_refresh=True).text)
            click.echo(f"{name}\t{location}\tOK")
        except StylesheetError as e:
            failed = True
            click.echo(f"{name}\t{location}\tERROR: {e}", err=True)
    if failed:
        sys.exit(1)


@xslt.command()
@click.argument("name")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-p", "--param", "params", multiple=True, help="key=value, 'true'/'false' are passed as booleans")
@with_appcontext
def test(name: str, input_file: Path, params: tuple[str, ...]) -> None:
    """Run a stylesheet on INPUT_FILE in the sandbox and print the result."""
    parsed: dict[str, str | bool] = {}
    for param in params:
        key, sep, value = param.partition("=")
        if not sep:
            raise click.BadParameter(f"{param!r} is not key=value", param_hint="--param")
        parsed[key] = {"true": True, "false": False}.get(value, value)
    transform = _transform(name)
    try:
        result = transform(parse_xml(input_file.read_bytes()), **parsed)
    except ValidationError as e:
        raise click.ClickException("\n".join(map(str, e.messages))) from e
    click.echo(etree.tostring(result, encoding=str, pretty_print=True))
