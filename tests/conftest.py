from __future__ import annotations

import os
from pathlib import Path

from mechanical_design_agent.config import load_env_file


_requested_env_file = os.environ.get("MECH_DESIGN_ENV_FILE", "").strip()
if _requested_env_file:
    load_env_file(Path(_requested_env_file).expanduser().resolve())
