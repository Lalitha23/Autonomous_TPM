from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from core.program_context import ProgramContext


def load_context(path: str) -> ProgramContext:
    """
    Load and validate a ProgramContext from a YAML file.

    Args:
        path: Absolute or relative path to the YAML config file.

    Returns:
        Fully populated and validated ProgramContext instance.

    Raises:
        FileNotFoundError: If the config file does not exist at the given path.
        ValueError: If the YAML is missing required fields or contains invalid values.
    """
    yaml_path = Path(path)
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"Program context file not found: {yaml_path.resolve()}"
        )

    with open(yaml_path, "r") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(
            f"Program context file must be a YAML mapping, got: {type(raw).__name__}"
        )

    try:
        return ProgramContext(**raw)
    except ValidationError as exc:
        # Surface the first error clearly rather than dumping the full Pydantic trace
        first_error = exc.errors()[0]
        field = " -> ".join(str(loc) for loc in first_error["loc"])
        msg = first_error["msg"]
        raise ValueError(
            f"Invalid program context in '{yaml_path.name}': "
            f"field '{field}' — {msg}"
        ) from exc
