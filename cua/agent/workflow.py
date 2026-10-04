"""The full discovery workflow: discover -> verify by replay -> save.

An artifact is only written to disk if replaying it, with no planner involved,
succeeds AND returns exactly the outputs the discovery run observed. A recording
that can't reproduce its own run is never trusted.
"""

from pathlib import Path
from typing import Literal

from cua.agent.discovery import Discoverer, DiscoveryResult, DiscoveryStatus
from cua.replay import Replayer, ReplayResult, ReplayStatus
from cua.schema import InputParam, StrictModel, save


class DiscoverReport(StrictModel):
    status: Literal["saved", "needs_human", "failed"]
    message: str
    discovery: DiscoveryResult | None = None
    verification: ReplayResult | None = None
    artifact: str | None = None


def discover_and_save(*, discoverer: Discoverer, replayer: Replayer, goal: str,
                      capability_id: str, inputs: list[InputParam],
                      input_values: dict[str, str], out_dir: Path) -> DiscoverReport:
    path = Path(out_dir) / f"{capability_id}@1.0.0.json"
    if path.exists():
        # Artifacts are versioned and reviewed: never silently replace one.
        return DiscoverReport(status="failed",
                              message=f"{path} already exists; choose a new capability id")

    discovery = discoverer.run(goal=goal, capability_id=capability_id,
                               inputs=inputs, input_values=input_values)
    if discovery.status == DiscoveryStatus.NEEDS_HUMAN:
        return DiscoverReport(status="needs_human", discovery=discovery,
                              message=discovery.intervention.reason)
    if discovery.status == DiscoveryStatus.FAILED:
        return DiscoverReport(status="failed", discovery=discovery, message=discovery.failure)

    # Prove the recording: replay it with the same inputs, no planner involved.
    verification = replayer.run(discovery.capability, input_values)
    if verification.status != ReplayStatus.SUCCESS:
        return DiscoverReport(status="failed", discovery=discovery, verification=verification,
                              message="verification replay did not succeed; artifact not saved")
    if verification.outputs != discovery.observed_outputs:
        return DiscoverReport(status="failed", discovery=discovery, verification=verification,
                              message="verification replay returned different outputs than "
                                      "discovery observed; artifact not saved")

    save(discovery.capability, path)
    return DiscoverReport(status="saved", discovery=discovery, verification=verification,
                          artifact=str(path),
                          message=f"recorded, verified by replay, saved as {path.name}")