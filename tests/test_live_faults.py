"""Replay against every fault, starting a fresh bank app for each test.

Each test launches its own copy of the app on a free port with exactly the faults it
needs, then shuts it down. No manual setup, no shared state between tests, and anyone
can reproduce the results with:  pytest tests/test_live_faults.py -v
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from cua.policy import Policy
from cua.replay import FailureCategory, Replayer, ReplayStatus
from cua.schema import load_app_profile, load_capability

pytestmark = pytest.mark.live

PROFILE = load_app_profile("artifacts/harbor-member-console/app_profile.json")
CAPABILITY = load_capability("artifacts/harbor-member-console/member.lookup_balance@1.0.0.json")
BASE_POLICY = Policy.load("policy.yaml")


def free_port() -> int:
    """Ask the operating system for a port nobody is using."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def bank_app(faults: str = "", slow_seconds: float | None = None):
    """Run a fresh bank app with the given faults for the duration of a `with` block."""
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


def replay(base_url: str, tmp_path: Path, member_id: str = "12345"):
    # A COPY of the real policy that also allows this test app's port. policy.yaml is untouched.
    config = BASE_POLICY.config.model_copy(update={"allowed_origins": [base_url]})
    replayer = Replayer(PROFILE, Policy(config), base_url, allow_draft=True, runs_dir=tmp_path)
    return replayer.run(CAPABILITY, {"member_id": member_id})


def test_system_notice_is_dismissed_and_the_run_succeeds(tmp_path):
    with bank_app("notice") as url:
        result = replay(url, tmp_path)
    assert result.status == ReplayStatus.SUCCESS, result.failure
    assert result.outputs == {"balance": "2450.75"}
    assert any("system_notice" in note for note in result.recoveries)


def test_expired_session_reauthenticates_without_repeating_the_search(tmp_path):
    with bank_app("timeout") as url:
        result = replay(url, tmp_path)
    assert result.status == ReplayStatus.SUCCESS, result.failure
    assert result.outputs == {"balance": "2450.75"}
    assert result.recoveries == [
        "recovered from 'session_expired' at step 'submit_search' (reauthenticate)"]
    events = (tmp_path / result.run_id / "events.jsonl").read_text()
    assert "met after recovery, action not repeated" in events


def test_slow_page_within_budget_succeeds_with_a_warning(tmp_path):
    with bank_app("slow", slow_seconds=4) as url:
        result = replay(url, tmp_path)
    assert result.status == ReplayStatus.SUCCESS, result.failure
    assert any("slow response" in warning for warning in result.warnings)


def test_page_slower_than_budget_is_a_retryable_timeout(tmp_path):
    with bank_app("slow", slow_seconds=12) as url:
        result = replay(url, tmp_path)
    assert result.status == ReplayStatus.FAILED
    assert result.failure.category == FailureCategory.TIMEOUT
    assert result.failure.retryable is True
    assert result.failure.step_id == "submit_search"


def test_core_outage_is_a_retryable_app_error_with_evidence(tmp_path):
    with bank_app("outage") as url:
        result = replay(url, tmp_path)
    assert result.status == ReplayStatus.FAILED
    assert result.failure.category == FailureCategory.APP_ERROR
    assert result.failure.retryable is True
    assert result.failure.step_id == "submit_search"
    assert Path(result.failure.screenshot).exists()


def test_faults_combine_cleanly(tmp_path):
    with bank_app("notice,timeout,slow", slow_seconds=3) as url:
        result = replay(url, tmp_path)
    assert result.status == ReplayStatus.SUCCESS, result.failure
    assert result.outputs == {"balance": "2450.75"}
    assert len(result.recoveries) == 2
    assert any("slow response" in warning for warning in result.warnings)