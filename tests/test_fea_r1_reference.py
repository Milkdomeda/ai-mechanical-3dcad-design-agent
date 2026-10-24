import pytest
from test_fea_mesh_quality import RIGHT_TET10


def tetra_input():
    return '*NODE, NSET=Nall\n'+''.join(f'{i}, {p[0]}, {p[1]}, {p[2]}\n' for i,p in enumerate(RIGHT_TET10,1))+'*ELEMENT, TYPE=C3D10, ELSET=Eall\n1, 1,2,3,4,5,6,7,8,9,10\n*CLOAD\n5,3,-13.333333333333333\n6,3,-13.333333333333333\n7,3,-13.333333333333333\n'


def test_reference_extracts_oriented_t6_boundary_and_duplicate_load_rows(tmp_path):
    from fea_r1_reference import read_native_input,boundary_faces
    p=tmp_path/'mesh.inp';p.write_text(tetra_input()+'5,3,1\n')
    nodes,elements,loads=read_native_input(p)
    faces=boundary_faces(elements)
    assert len(nodes)==10 and len(faces)==4
    assert (1,2,3,5,6,7) in faces
    assert loads[5][2]==pytest.approx(-12.333333333333333)


@pytest.mark.parametrize('defect',['missing_node','duplicate_node','wrong_element','nonfinite_load'])
def test_reference_rejects_ambiguous_or_incomplete_input(tmp_path,defect):
    from fea_r1_reference import read_native_input
    text=tetra_input()
    if defect=='missing_node':text=text.replace('10, 0, 0.5, 0.5\n','')
    if defect=='duplicate_node':text=text.replace('*ELEMENT','1, 0,0,0\n*ELEMENT')
    if defect=='wrong_element':text=text.replace('C3D10','C3D4')
    if defect=='nonfinite_load':text=text.replace('-13.333333333333333','nan')
    p=tmp_path/'bad.inp';p.write_text(text)
    with pytest.raises(ValueError,match='R1_REFERENCE'):read_native_input(p)


def supported_input():
    return tetra_input().replace('*CLOAD','*NSET,NSET=Fixed\n1,5\n*STEP\n*STATIC\n*BOUNDARY\nFixed,1,3\n*CLOAD')+'*NODE PRINT,NSET=Fixed,TOTALS=ONLY\nRF\n*END STEP\n'


def test_rf_accounting_uses_declared_fixed_dofs_and_written_cloads(tmp_path):
    from fea_r1_reference import support_load_accounting
    p=tmp_path/'support.inp';p.write_text(supported_input())
    value=support_load_accounting(p)
    assert value['constrained_cload_N']==pytest.approx([0,0,-40/3])
    assert value['free_cload_N']==pytest.approx([0,0,-80/3])
    assert value['applied_cload_N']==pytest.approx([0,0,-40])
    assert value['rf_nset']=='FIXED' and value['constrained_nodes']==2


@pytest.mark.parametrize('defect',['transform','distributed','partial','nonzero','missing_print','multiple_steps'])
def test_rf_accounting_rejects_unsupported_force_semantics(tmp_path,defect):
    from fea_r1_reference import support_load_accounting
    text=supported_input()
    if defect=='transform':text+='*TRANSFORM,NSET=Fixed\n'
    if defect=='distributed':text+='*DLOAD\n'
    if defect=='partial':text=text.replace('Fixed,1,3','Fixed,1,2')
    if defect=='nonzero':text=text.replace('Fixed,1,3','Fixed,1,3,0.1')
    if defect=='missing_print':text=text.replace('*NODE PRINT,NSET=Fixed,TOTALS=ONLY','*NODE FILE')
    if defect=='multiple_steps':text+='*STEP\n*STATIC\n*END STEP\n'
    p=tmp_path/'unsupported.inp';p.write_text(text)
    with pytest.raises(ValueError,match='R1_RF_'):support_load_accounting(p)
