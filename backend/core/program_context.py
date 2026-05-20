from __future__ import annotations

from datetime import date
from typing import List

from pydantic import BaseModel, Field


class TeamConfig(BaseModel):
    name: str
    lead: str
    capacity_points: int
    ownership_areas: List[str]


class MilestoneConfig(BaseModel):
    id: str
    name: str
    target_date: date
    blocking_milestone_ids: List[str] = Field(default_factory=list)


class ThresholdConfig(BaseModel):
    stale_ticket_days: int
    overload_points_per_assignee: int
    scope_creep_story_point_increase: float
    critical_path_severity_multiplier: float


class SprintHealthRules(BaseModel):
    watch_min_flags: int
    alert_requires_severity: str   # HIGH
    escalate_requires_severity: str  # CRITICAL


class EscalationRules(BaseModel):
    trigger_severities: List[str]   # e.g. [HIGH, CRITICAL]
    trigger_flags: List[str]        # e.g. [BLOCKED, STALE]


class BaselineConfig(BaseModel):
    normal_velocity_points: int
    avg_blocker_resolution_days: float
    historical_spillover_rate: float
    normal_dependency_latency_days: float


class SimulationWeights(BaseModel):
    ticket_stalled: float = 0.15
    blocker_added: float = 0.08
    scope_expanded: float = 0.06
    ticket_progressed: float = 0.25
    blocker_resolved: float = 0.20


class ProgramContext(BaseModel):
    # Program metadata
    program_id: str
    program_name: str
    domain: str
    launch_target: date
    business_priority: str
    release_stage: str

    # Team topology
    teams: List[TeamConfig]

    # Critical path milestones
    critical_path: List[MilestoneConfig]

    # Operational thresholds
    thresholds: ThresholdConfig

    # Sprint health rules
    sprint_health_rules: SprintHealthRules

    # Escalation rules
    escalation_rules: EscalationRules

    # Historical baselines
    baselines: BaselineConfig

    # Detection tool registry
    # Default for enterprise_software:
    # ["detect_stale_tickets", "detect_blocked_tickets",
    #  "detect_scope_creep", "detect_overloaded_assignees"]
    detection_tools: List[str]

    # Simulation weights — optional, defaults match Simulation Engine base probabilities
    simulation_weights: SimulationWeights = Field(default_factory=SimulationWeights)
