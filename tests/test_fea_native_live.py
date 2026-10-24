from __future__ import annotations

import os
from pathlib import Path

import pytest

from fea_native_probe import parse_config, probe_native


@pytest.mark.live_fea
@pytest.mark.skipif(os.environ.get("MECH_DESIGN_FEA_LIVE_TESTS") != "1", reason="Explicit isolated P0 live opt-in required")
def test_native_fea_automated_subset(tmp_path: Path) -> None:
    config = parse_config(Path(os.environ["MECH_DESIGN_FEA_CONFIG"]))
    report = probe_native(config, tmp_path / "native")
    assert report["automated_status"] == "passed", report
    assert report["checks"]["native_startup_cancellation"]["status"] == "passed"
    # This test intentionally does not assert the complete P0 gate passed.
