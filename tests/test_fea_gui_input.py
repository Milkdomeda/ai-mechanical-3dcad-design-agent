from pathlib import Path
import pytest


INPUT = '''** independent one-tetrahedron software fixture
*NODE,NSET=ALL
1,0,0,0
2,1,0,0
3,0,1,0
4,0,0,1
5,.5,0,0
6,.5,.5,0
7,0,.5,0
8,0,0,.5
9,.5,0,.5
10,0,.5,.5
*ELEMENT,TYPE=C3D10,ELSET=VOLUME
1,1,2,3,4,5,6,7,8,9,10
*ELSET,ELSET=BODY
VOLUME
*NSET,NSET=FIXED
1,2,3
*MATERIAL,NAME=STEEL
*ELASTIC
200000,.3
*EXPANSION,ZERO=0
0
*SOLID SECTION,ELSET=BODY,MATERIAL=STEEL
*STEP,INC=2000
*STATIC
1,1,1e-5,1
*BOUNDARY
FIXED,1,3
*CLOAD
5,3,-20
7,3,-20
*NODE FILE
U
*EL FILE
S,E
*NODE PRINT,NSET=FIXED,TOTALS=ONLY
RF
*OUTPUT,FREQUENCY=1
*END STEP
'''


def compare(tmp_path, other):
    from fea_gui_input import compare_inputs
    a=tmp_path/'reference.inp';b=tmp_path/'exported.inp'
    a.write_text(INPUT);b.write_text(other)
    return compare_inputs(a,b)


def test_equal_semantics_reports_independent_resultants(tmp_path):
    result=compare(tmp_path,INPUT.replace('200000,.3','2.0e5,0.30').replace('** independent','** new comment independent'))
    assert result['status']=='passed'
    assert result['exported']['force_N']==[0.,0.,-40.]
    assert result['exported']['moment_Nmm']==[-10.,10.,0.]
    assert result['exported']['quadratic_virtual_work_Nmm']==pytest.approx(-.05)


def test_bijective_node_element_and_set_renumbering_preserves_semantics(tmp_path):
    lines=[];section=''
    for line in INPUT.splitlines():
        if line.startswith('*'):
            section=line.split(',')[0];lines.append(line.replace('ALL','NODES').replace('VOLUME','CELLS').replace('BODY','DOMAIN').replace('FIXED','CLAMP').replace('STEEL','MAT'));continue
        cells=line.split(',')
        if section=='*NODE':cells[0]=str(int(cells[0])+100)
        elif section=='*ELEMENT':cells=[str(int(v)+100) for v in cells]
        elif section=='*NSET':cells=[str(int(v)+100) for v in cells]
        elif section=='*CLOAD':cells[0]=str(int(cells[0])+100)
        line=','.join(cells).replace('VOLUME','CELLS').replace('FIXED','CLAMP')
        lines.append(line)
    assert compare(tmp_path,'\n'.join(lines))['status']=='passed'


@pytest.mark.parametrize('old,new',[
    ('200000,.3','100000,.3'),('FIXED,1,3','FIXED,1,2'),
    ('5,3,-20\n7,3,-20','5,3,-10\n7,3,-30'),
    ('1,1,2,3,4,5,6,7,8,9,10','1,1,2,3,4,6,5,7,8,9,10'),
    ('*NODE FILE\nU','*NODE FILE\nRF'),
    ('*STATIC\n1,1,1e-5,1','*STATIC\n1,2,1e-5,1'),
    ('*SOLID SECTION,ELSET=BODY,MATERIAL=STEEL','*SOLID SECTION,ELSET=FIXED,MATERIAL=STEEL'),
    ('5,.5,0,0','5,.50000001,0,0'),
])
def test_material_boundary_distribution_connectivity_and_step_changes_fail(tmp_path,old,new):
    assert compare(tmp_path,INPUT.replace(old,new))['status']=='failed'


@pytest.mark.parametrize('text',[
    INPUT+'*INCLUDE,INPUT=external.inp\n',INPUT+'*TRANSFORM,NSET=ALL\n1,0,0,0,1,0\n',
    INPUT.replace('5,.5,0,0','5,nan,0,0'),
    INPUT.replace('5,.5,0,0','5,0,0,0'),
    INPUT.replace('*CLOAD','*CLOAD,AMPLITUDE=HIDDEN'),
    INPUT.replace('*END STEP',''),INPUT.replace('200000,.3',''),
    INPUT.replace('1,2,3\n*MATERIAL','999\n*MATERIAL'),
])
def test_unsupported_incomplete_nonfinite_or_ambiguous_input_is_rejected(tmp_path,text):
    assert compare(tmp_path,text)['status']=='failed'


def test_generate_sets_and_repeated_cloads_are_semantic(tmp_path):
    other=INPUT.replace('*NSET,NSET=FIXED\n1,2,3','*NSET,NSET=FIXED,GENERATE\n1,3,1')
    other=other.replace('5,3,-20','5,3,-10\n5,3,-10')
    assert compare(tmp_path,other)['status']=='passed'
