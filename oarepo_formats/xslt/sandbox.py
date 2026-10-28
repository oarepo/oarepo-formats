# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Sandbox worker: runs untrusted XSLT stylesheets in SaxonC-HE.

Internal: started by [oarepo_formats.xslt.pool][] as a script (`python -I sandbox.py [max_memory]`)
with an empty environment and an empty working directory. It must not import anything from
`oarepo_formats` (or flask, invenio, ...) - only the standard library and `saxonche`.

Saxon's `allowedProtocols` is set to nothing, so a stylesheet cannot read files, reach the network or
use `xsl:include`/`xsl:import`; `xsl:result-document` output is kept in memory and discarded.

Protocol: JSON lines on stdin/stdout, one request line -> one response line.

Request:

```python
{
    "stylesheet": str,
    "xml": str | None,
    "params": {name: str | bool},
}
```

The stylesheet text is sent with every request; compiled stylesheets are cached by text.
`xml: None` only compiles the stylesheet (used to validate a new stylesheet version).

Response:

```python
{
    "ok": True,
    "xml": str | None,
    "messages": [str],
    "result_documents": [uri],
}
{
    "ok": False,
    "error": str,
    "messages": [str],
}
```
"""

from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from typing import Any

ALLOWED_PROTOCOLS = "http://saxon.sf.net/feature/allowedProtocols"
CACHE_SIZE = 8


def limit_memory(max_memory: int) -> None:
    """Cap the data segment of this process (Linux; not enforceable on all platforms)."""
    import resource

    try:
        resource.setrlimit(resource.RLIMIT_DATA, (max_memory, max_memory))
    except (ValueError, OSError) as e:
        print(f"oarepo-formats sandbox: cannot limit memory: {e}", file=sys.stderr)  # noqa: T201


class Sandbox:
    """Saxon processor with no URI access and an LRU of compiled stylesheets."""

    def __init__(self) -> None:
        """Start Saxon."""
        from saxonche import PySaxonProcessor

        self.processor = PySaxonProcessor(license=False)
        self.processor.set_configuration_property(ALLOWED_PROTOCOLS, "")
        self.compiler = self.processor.new_xslt30_processor()
        # per instance, so the cache does not keep the processor alive (ruff B019)
        self.compile = lru_cache(maxsize=CACHE_SIZE)(self._compile)

    def _compile(self, stylesheet: str) -> Any:
        """Compile a stylesheet; errors are not cached, so a failing stylesheet is retried."""
        executable = self.compiler.compile_stylesheet(stylesheet_text=stylesheet)
        executable.set_capture_result_documents(True)
        executable.set_save_xsl_message(True)
        return executable

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Compile (if needed) and run a stylesheet."""
        from saxonche import PySaxonApiError

        try:
            executable = self.compile(request["stylesheet"])
        except PySaxonApiError as e:
            return {"ok": False, "error": f"Cannot compile stylesheet: {e}", "messages": []}

        if request.get("xml") is None:
            return {"ok": True, "xml": None, "messages": [], "result_documents": []}

        executable.clear_parameters()
        executable.clear_xsl_messages()
        for name, value in (request.get("params") or {}).items():
            xdm = (
                self.processor.make_boolean_value(value)
                if isinstance(value, bool)
                else self.processor.make_string_value(str(value))
            )
            executable.set_parameter(name, xdm)
        try:
            document = self.processor.parse_xml(xml_text=request["xml"])
            result = executable.transform_to_string(xdm_node=document)
        except PySaxonApiError as e:
            return {"ok": False, "error": str(e).strip(), "messages": self.messages(executable)}
        # secondary result documents are kept in memory by Saxon and discarded here
        result_documents = list(executable.get_result_documents() or {})
        return {
            "ok": True,
            "xml": result,
            "messages": self.messages(executable),
            "result_documents": result_documents,
        }

    @staticmethod
    def messages(executable: Any) -> list[str]:
        """Return xsl:message output of the last transformation."""
        messages = executable.get_xsl_messages()
        return [str(m) for m in messages] if messages is not None else []


def main() -> None:
    """Serve requests until stdin is closed."""
    if len(sys.argv) > 1:
        limit_memory(int(sys.argv[1]))
    stdin = sys.stdin.buffer
    # keep the protocol stream on a private fd; anything printed by Saxon or python goes to stderr
    stdout = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    sandbox = Sandbox()
    for line in stdin:
        try:
            response = sandbox.handle(json.loads(line))
        except Exception as e:  # noqa: BLE001 - report everything to the parent
            response = {"ok": False, "error": f"{type(e).__name__}: {e}", "messages": []}
        # json.dumps escapes newlines, so a message is always a single line
        stdout.write(json.dumps(response).encode("utf-8") + b"\n")
        stdout.flush()


if __name__ == "__main__":
    main()
