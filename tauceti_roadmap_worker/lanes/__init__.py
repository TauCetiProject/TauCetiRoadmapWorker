"""One lane per job kind. Lanes are TauCetiWorker-style `only=` kinds: a worker can be pinned to
some of them (`work --only review,redteam`) or take whatever is most useful."""

from __future__ import annotations

from ..board import Job, parse_job_id
from .base import Context, Lane, LaneError, LaneResult
from .design import DesignLane
from .fix import ArtefactFixLane, RedTeamFixLane
from .gate import GateLane
from .package import PackageLane
from .port import PortLane
from .redteam import RedTeamLane
from .review import ReviewLane
from .target import TargetLane

LANES = {"target": TargetLane, "design": DesignLane, "package": PackageLane, "review": ReviewLane,
         "port": PortLane, "redteam": RedTeamLane, "gate": GateLane}


def lane_for(job: Job) -> Lane:
    if job.kind == "fix":
        return RedTeamFixLane() if parse_job_id(job.id).subject == "RT" else ArtefactFixLane()
    return LANES[job.kind]()


__all__ = ["Context", "Lane", "LaneError", "LaneResult", "lane_for", "LANES"]
