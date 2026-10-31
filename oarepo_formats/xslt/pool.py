# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Pool of sandbox worker processes (see [oarepo_formats.xslt.sandbox][]).

Internal: used through [XSLTTransform][oarepo_formats.xslt.transform.XSLTTransform]; the pool of the
current process is [current_formats.xslt_pool][oarepo_formats.ext.OARepoFormats.xslt_pool].

Workers are separate processes started with `python -I`, an empty environment and an empty temporary
working directory, so a stylesheet cannot see the environment of the web/celery process. A call that
exceeds the timeout kills the worker; a new one is started on the next call. `subprocess` is used
instead of `multiprocessing` because celery prefork workers are daemonic and cannot have
`multiprocessing` children.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import selectors
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SANDBOX_SCRIPT = str(Path(__file__).with_name("sandbox.py"))


def python_executable() -> str:
    """Return the Python interpreter to start a sandbox worker with.

    Under uwsgi `sys.executable` is the uwsgi binary itself (uwsgi sets the program name to its own
    path), so it can not run a script; the interpreter of `sys.prefix` is used instead, which keeps
    the virtualenv of the application.
    """
    if Path(sys.executable).name.startswith("python"):
        return sys.executable
    scripts = Path(sysconfig.get_path("scripts"))
    for name in ("python3", "python", "python.exe"):
        if (candidate := scripts / name).exists():
            return str(candidate)
    return sys.executable


class SandboxError(Exception):
    """The sandbox worker timed out, crashed or returned garbage."""


class SandboxWorker:
    """One sandbox worker process."""

    def __init__(self, max_memory: int | None = None) -> None:
        """Start the worker process."""
        self.cwd = tempfile.TemporaryDirectory(prefix="oarepo-xslt-")
        args = [python_executable(), "-I", SANDBOX_SCRIPT]
        if max_memory:
            args.append(str(max_memory))
        self.process = subprocess.Popen(  # noqa: S603 - fixed command line
            args,
            env={},
            cwd=self.cwd.name,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            close_fds=True,
        )

    @property
    def alive(self) -> bool:
        """Return True if the worker process is running."""
        return self.process.poll() is None

    def call(self, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Send a request and wait at most `timeout` seconds for the response."""
        deadline = time.monotonic() + timeout
        try:
            stdin = self.process.stdin
            assert stdin is not None  # noqa: S101 - always set with stdin=PIPE
            stdin.write(json.dumps(request).encode("utf-8") + b"\n")
            stdin.flush()
            return json.loads(self._readline(deadline))
        except (OSError, ValueError) as e:
            raise SandboxError(f"XSLT sandbox failed: {e}") from e

    def _readline(self, deadline: float) -> bytes:
        """Read one response line; the worker writes nothing after it until the next request."""
        stdout = self.process.stdout
        assert stdout is not None  # noqa: S101 - always set with stdout=PIPE
        fd = stdout.fileno()
        chunks: list[bytes] = []
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while not chunks or not chunks[-1].endswith(b"\n"):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise SandboxError("XSLT sandbox timed out")
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    raise SandboxError(f"XSLT sandbox exited with code {self.process.wait()}")
                chunks.append(chunk)
        return b"".join(chunks)

    def kill(self) -> None:
        """Kill the worker process and remove its working directory."""
        self.process.kill()
        self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
        self.cwd.cleanup()


class SandboxPool:
    """Workers handed out from a queue, so that threads do not share a pipe."""

    def __init__(self, size: int = 1, timeout: float = 30, max_memory: int | None = None) -> None:
        """Create the pool; workers are started lazily."""
        self.pid = os.getpid()
        self.timeout = timeout
        self.max_memory = max_memory
        self.workers: queue.Queue[SandboxWorker | None] = queue.Queue()
        for _ in range(size):
            self.workers.put(None)

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run a request in a free worker, waiting for one if all are busy.

        Raises:
            SandboxError: on timeout or crash; the worker is killed.

        """
        worker = self.workers.get()
        try:
            if worker is None or not worker.alive:
                worker = SandboxWorker(self.max_memory)
            return worker.call(request, self.timeout)
        except SandboxError:
            if worker is not None:
                worker.kill()
            worker = None
            raise
        finally:
            self.workers.put(worker)

    def close(self) -> None:
        """Kill all idle workers; they are restarted on the next call."""
        workers = []
        while True:
            try:
                workers.append(self.workers.get_nowait())
            except queue.Empty:
                break
        for worker in workers:
            if worker is not None:
                worker.kill()
            self.workers.put(None)
