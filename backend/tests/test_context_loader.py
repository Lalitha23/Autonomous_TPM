from pathlib import Path

import pytest

from core.context_loader import load_context
from core.program_context import ProgramContext

# Resolve path to default.yaml relative to this file
DEFAULT_YAML = Path(__file__).parent.parent / "config" / "programs" / "default.yaml"

EXPECTED_DETECTION_TOOLS = [
    "detect_stale_tickets",
    "detect_blocked_tickets",
    "detect_scope_creep",
    "detect_overloaded_assignees",
]


def test_default_yaml_loads_successfully():
    """default.yaml must parse into a valid ProgramContext without errors."""
    ctx = load_context(str(DEFAULT_YAML))
    assert isinstance(ctx, ProgramContext)


def test_program_name():
    ctx = load_context(str(DEFAULT_YAML))
    assert ctx.program_name == "Project Atlas"


def test_domain():
    ctx = load_context(str(DEFAULT_YAML))
    assert ctx.domain == "enterprise_software"


def test_teams_count():
    ctx = load_context(str(DEFAULT_YAML))
    assert len(ctx.teams) == 3


def test_critical_path_count():
    ctx = load_context(str(DEFAULT_YAML))
    assert len(ctx.critical_path) == 4


def test_stale_ticket_days():
    ctx = load_context(str(DEFAULT_YAML))
    assert ctx.thresholds.stale_ticket_days == 3


def test_all_detection_tools_present():
    ctx = load_context(str(DEFAULT_YAML))
    for tool in EXPECTED_DETECTION_TOOLS:
        assert tool in ctx.detection_tools, f"Missing detection tool: {tool}"


def test_missing_file_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_context("/nonexistent/path/to/config.yaml")


def test_team_names():
    ctx = load_context(str(DEFAULT_YAML))
    names = [t.name for t in ctx.teams]
    assert "Platform" in names
    assert "Payments" in names
    assert "Mobile" in names


def test_milestone_sequence():
    ctx = load_context(str(DEFAULT_YAML))
    ids = [m.id for m in ctx.critical_path]
    assert ids == ["m1", "m2", "m3", "m4"]


def test_milestone_blocking_chain():
    ctx = load_context(str(DEFAULT_YAML))
    milestones = {m.id: m for m in ctx.critical_path}
    assert milestones["m1"].blocking_milestone_ids == []
    assert "m1" in milestones["m2"].blocking_milestone_ids
    assert "m2" in milestones["m3"].blocking_milestone_ids
    assert "m3" in milestones["m4"].blocking_milestone_ids


def test_simulation_weights_defaults():
    ctx = load_context(str(DEFAULT_YAML))
    assert ctx.simulation_weights.ticket_stalled == 0.15
    assert ctx.simulation_weights.ticket_progressed == 0.25
