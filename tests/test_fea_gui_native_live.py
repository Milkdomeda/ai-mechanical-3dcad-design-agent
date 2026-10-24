"""Opt-in native GUI automation; no mocks establish display compatibility."""
import json
import os
from pathlib import Path
import pytest


@pytest.mark.live_fea
def test_native_gui_fixture_has_standard_panels_and_equivalent_button_export(tmp_path):
    config=os.environ.get('FEA_GUI_LIVE_CONFIG')
    scratch=os.environ.get('FEA_GUI_LIVE_SCRATCH')
    if not config or not scratch:pytest.skip('requires explicitly configured isolated GUI runtime and W1 root')
    from fea_gui_automation import run_gui_automation
    report=run_gui_automation(json.loads(Path(config).read_text(encoding='utf-8')),tmp_path/'GUI 中文',Path(scratch),case_ids=('axial_bar',))
    assert report['actor']=='assistant'
    assert report['automated_status']=='passed',report
    assert report['cases']['axial_bar']['input_comparison']['status']=='passed'
    assert report['cases']['axial_bar']['gui_objects']['status']=='passed'
    assert report['cases']['axial_bar']['native']['source_unchanged'] is True
    assert report['runtime_unchanged'] is True
    assert report['passes']['build']['startup_pid']!=report['passes']['roundtrip']['startup_pid']
    assert report['passes']['roundtrip']['producer']['sha256']==report['passes']['build']['native']['sha256']
