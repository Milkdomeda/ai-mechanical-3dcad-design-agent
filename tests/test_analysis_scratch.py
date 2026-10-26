"""A busy native root is cancellable before acquiring another W1 owner."""
import pytest

from mechanical_design_agent import secure_fs
from mechanical_design_agent.analysis_types import AnalysisError


def test_waiting_for_an_owned_native_root_obeys_cancellation(tmp_path):
    from mechanical_design_agent.analysis_scratch import _owner_lock
    lock=tmp_path/'owner.lock';calls=[]
    def checkpoint():
        calls.append(1)
        if len(calls)==2:raise AnalysisError('ANALYSIS_CANCELLED','Synthetic cancellation while waiting for W1.')
    with secure_fs.exclusive_file_lock(lock):
        with pytest.raises(AnalysisError) as error:
            with _owner_lock(lock,checkpoint):pytest.fail('cannot acquire another owner lock')
    assert error.value.code=='ANALYSIS_CANCELLED'
    assert len(calls)==2
