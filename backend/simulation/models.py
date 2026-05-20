"""
Simulation Engine — Pydantic models for in-memory ticket and sprint state.

These are NOT SQLAlchemy models. They represent the live simulation state
that the engine mutates each cycle before persisting to PostgreSQL.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class SimTicket(BaseModel):
    """In-memory representation of a single Jira-like ticket."""

    id: str                             # e.g. "ATLAS-001"
    title: str
    status: str                         # TODO|IN_PROGRESS|IN_REVIEW|BLOCKED|DONE
    priority: str                       # P0|P1|P2|P3
    assignee: str
    team: str
    sprint_id: str
    story_points: int
    points_completed: int = 0
    is_on_critical_path: bool = False
    blocker_ids: List[str] = Field(default_factory=list)
    stale_since: Optional[datetime] = None
    scope_changed: bool = False
    milestone_target: Optional[str] = None
    risk_flag: Optional[str] = None     # STALE|BLOCKED|SCOPE_CREEP|OVERLOADED
    risk_severity: Optional[str] = None  # LOW|MEDIUM|HIGH|CRITICAL
    risk_reason: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(frozen=False)


class SimSprint(BaseModel):
    """In-memory representation of a sprint."""

    id: str                             # e.g. "sprint-1"
    name: str                           # e.g. "Sprint 1"
    start_date: datetime
    end_date: datetime
    tickets: List[SimTicket] = Field(default_factory=list)

    model_config = ConfigDict(frozen=False)

    # ── Derived helpers ──────────────────────────────────────────────────────

    @property
    def total_points(self) -> int:
        return sum(t.story_points for t in self.tickets)

    @property
    def completed_points(self) -> int:
        return sum(t.points_completed for t in self.tickets)

    @property
    def completion_rate(self) -> float:
        total = self.total_points
        return self.completed_points / total if total > 0 else 0.0
