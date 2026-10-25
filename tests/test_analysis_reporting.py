from fea_evaluation_fixture import evaluation_spec


def test_report_retains_scope_failure_staleness_and_exact_observation_numbers():
    from mechanical_design_agent.analysis_reporting import build_report
    spec=evaluation_spec()
    result={'numerical_status':'passed','criteria_status':'not_evaluated','applicability_status':'limited_screening','currentness':'source_changed','run_id':'synthetic-run'}
    level={'case_id':'static','level':2,'mesh':{'node_count':1000,'element_count':400,'size_mm':'1.96'},'observables':[{'id':'midspan-stress','region_id':'midspan-roi','value':1.23456789012345,'unit':'MPa','location':{'node_id':5,'coordinates_mm':[50.,2.5,2.5]}}],'raw_global_peaks':{'maximum_von_mises_MPa':1000.},'checks':[],'applicability':{'limitations':['No printed layer strength.']}}
    receipts={'attempt':{'numerical':{'finest':[level],'comparisons':[]},'criteria':{'checks':[]}},'images':{'static':['gui/outputs/static/von_mises-figure.png']}}
    report=build_report(spec,result,receipts)
    for text in ['1.23456789012345','1000.0','[50.0, 2.5, 2.5]','source_changed','not_evaluated','limited_screening','not an engineering certification','native_extrapolated_averaged_nodal','gui/outputs/static/von_mises-figure.png']:
        assert text in report
    assert 'safety factor' not in report.lower()


def test_report_escapes_untrusted_assumption_text():
    from mechanical_design_agent.analysis_reporting import _text
    assert '<script>' not in _text('<script>alert(1)</script>')
    assert '[link](javascript:' not in _text('[link](javascript:alert(1))')
