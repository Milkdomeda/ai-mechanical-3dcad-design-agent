"""Owned fresh W1 solver qualification; never mutates frozen native input."""
from pathlib import Path


def solve_and_extract(deck, bundle, settings, native, output):
    from mechanical_design_agent.analysis_results import extract_fields
    from mechanical_design_agent.analysis_solver import solve_native
    output=Path(output)
    outputs=solve_native(deck,bundle,settings,output,native=native)
    return extract_fields(deck,outputs,settings,output.parent/(output.name+' fields'),transaction=native)


def verify_synthetic_balance(result, deck, validation):
    import math
    for name,unit in [('force','N'),('moment','Nmm')]:
        actual=result['reactions'][name+'_'+unit]
        expected=list(map(float,deck['contract']['case']['total_'+name+'_'+unit]))
        residual=math.sqrt(sum((a+b)**2 for a,b in zip(actual,expected)))
        scale=max(math.sqrt(sum(v*v for v in actual)),math.sqrt(sum(v*v for v in expected)))
        assert residual<=float(validation[name+'_atol']['value'])+float(validation[name+'_rtol']['value'])*scale, (name,actual,expected,residual,scale)
