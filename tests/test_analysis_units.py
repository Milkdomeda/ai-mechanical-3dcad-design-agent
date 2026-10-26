from copy import deepcopy

import pytest


@pytest.mark.parametrize('value,unit,dimension,expected,base', [
    ('1250','kg/m^3','density','0.00000000125','tonne/mm^3'),
    ('1','m/s^2','acceleration','1000','mm/s^2'),
    ('1','kg','mass','0.001','tonne'),
    ('2.5','m','length','2500','mm'),
    ('200000000000','Pa','stress','200000','MPa'),
    ('0.2','GPa','stress','200','MPa'),
    ('3','N*m','moment','3000','N*mm'),
    ('1','g/cm^3','density','0.000000001','tonne/mm^3'),
    ('1','m^3','volume','1000000000','mm^3'),
    ('1','m^2','area','1000000','mm^2'),
    ('-0.000','1','dimensionless','0','1'),
])
def test_consistent_n_mm_s_tonne_conversion_retains_input(value,unit,dimension,expected,base):
    from mechanical_design_agent.analysis_units import normalize_quantity
    result=normalize_quantity(value,unit,dimension=dimension)
    assert result['value']==expected and result['unit']==base
    assert result['input']['unit']==unit


@pytest.mark.parametrize('value,unit,dimension',[
    (True,'N','force'),(1,'N','force'),(float('nan'),'N','force'),
    ('NaN','N','force'),('Infinity','N','force'),('1e99999','N','force'),
    ('1_000','N','force'),('1','','force'),('1','kg','force'),
    ('1','N','unknown'),('1','inch','length'),('1e-99999','N','force'),
])
def test_quantities_reject_ambiguous_nonfinite_or_unbounded_values(value,unit,dimension):
    from mechanical_design_agent.analysis_types import AnalysisError
    from mechanical_design_agent.analysis_units import normalize_quantity
    with pytest.raises(AnalysisError):normalize_quantity(value,unit,dimension=dimension)


@pytest.mark.parametrize('value,expected', [('1.000','1'),('+1e-3','0.001'),('-0e5','0'),('0.10','0.1')])
def test_decimal_canonicalization_is_exact(value,expected):
    from mechanical_design_agent.analysis_units import canonical_decimal
    assert canonical_decimal(value)==expected
