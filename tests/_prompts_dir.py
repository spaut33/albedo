"""Resolve the bundled prompts dir for tests via importlib.resources.

Lives outside the unit/integration trees so both can import it without
polluting the package or duplicating logic.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path


def bundled_prompts_dir() -> Path:
    """Path to `src/albedo/_data/prompts/` (or its installed equivalent)."""
    return Path(str(resources.files('albedo._data').joinpath('prompts')))
