"""Start a fresh copy of the bank app on a free port, with chosen faults.

Used by the evidence generator (python -m cua.demo). Each copy has its own clean data
and session state, and is always shut down afterwards.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager


def free_port() -> int:
    """Ask the operating system for a port nobody is using."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running_bank_app(faults: str = "", slow_seconds: float | None = None) -> Iterator[str]:
    """Yield the base URL of a fresh bank app running with the given faults."""
    port = free_port()
    env = {**os.environ, "BANK_FAULTS": faults}
    if slow_seconds is not None:
        env["BANK_SLOW_SECONDS"] = str(slow_seconds)
    process = subprocess.Popen(
        [sys.executable, "-m", "flask", "--app", "bank_app.app", "run", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while True:  # wait until the app answers
            try:
                urllib.request.urlopen(base_url + "/login", timeout=1)
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"bank app with faults={faults!r} did not start") from None
                time.sleep(0.2)
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=5)