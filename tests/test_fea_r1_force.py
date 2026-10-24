import math
import runpy
from types import SimpleNamespace
import pytest
from test_fea_r1_weights import PAYLOAD


class Quantity(float):
    def __mul__(self,other):return Quantity(float(self)*other)
    def __rmul__(self,other):return Quantity(other*float(self))
    @property
    def Value(self):return float(self)
    def getValueAs(self,unit):
        assert unit=='N'
        return self


def fixture():
    # Only FreeCAD's external unit/reference/mesh API is doubled. The actual
    # native T6 and global face-force functions run, returning real load tables.
    ns=runpy.run_path(str(PAYLOAD))
    points={1:(0,0,0),2:(1,0,0),3:(0,1,0),4:(.5,0,0),5:(.5,.5,0),6:(0,.5,0),
            7:(0,-2,0),8:(0,-1,0),9:(.5,-1,0)}
    tables={'A':{1:(1,2,3,4,5,6)},'B':{2:(1,7,2,8,9,4)}}
    ns['FreeCAD']=SimpleNamespace(Units=SimpleNamespace(Quantity=Quantity))
    ns['sub_shape_at_global_placement']=lambda obj,name:SimpleNamespace(ShapeType='Face',Area=100.,table=tables[name])
    ns['get_ref_facenodes_table']=lambda mesh,elements,face:face.table
    ns['get_ref_facenodes_areas']=lambda nodes,table:[(n,w) for face in table.values() for n,w in zip(face,ns['t6_weights']([nodes[i] for i in face]))]
    # runpy returns a copy of globals; rebind the real function to these API doubles.
    import types
    force=types.FunctionType(ns['get_force_obj_face_nodeload_table'].__code__,ns)
    obj=SimpleNamespace(Name='Solid')
    load=SimpleNamespace(Name='Load',Force=Quantity(40),References=[(obj,('A','B'))])
    return force,points,tables,obj,load


def test_all_faces_share_one_total_force_and_shared_nodes_accumulate():
    force,nodes,_,_,load=fixture();tables=force(None,{},nodes,load)
    sums=[math.fsum(float(v) for v in table.values()) for _,table in tables]
    assert sums==pytest.approx((40/3,80/3),rel=1e-12)
    assert math.fsum(sums)==pytest.approx(40,rel=1e-12)
    assert math.fsum(float(table.get(4,0)) for _,table in tables)==pytest.approx(40/3,rel=1e-12)


def test_reference_order_does_not_change_aggregated_nodal_loads():
    force,nodes,_,obj,load=fixture()
    def aggregate():
        entries=force(None,{},nodes,load)
        return {n:math.fsum(float(t.get(n,0)) for _,t in entries) for n in nodes}
    before=aggregate();load.References=[(obj,('B','A'))]
    assert aggregate()==pytest.approx(before,abs=1e-12)


@pytest.mark.parametrize('defect',['duplicate','alias','empty','no_mesh','nonfinite_force'])
def test_invalid_reference_cannot_emit_a_partial_force_table(defect):
    force,nodes,tables,obj,load=fixture()
    if defect=='duplicate':load.References=[(obj,('A','A'))]
    if defect=='alias':tables['B']=tables['A']
    if defect=='empty':load.References=[]
    if defect=='no_mesh':tables['B']={}
    if defect=='nonfinite_force':load.Force=Quantity(math.nan)
    with pytest.raises(ValueError,match='R1_'):force(None,{},nodes,load)


@pytest.mark.parametrize('scale',[1e-154,1e154])
def test_finite_total_force_survives_extreme_representable_area(scale):
    force,nodes,_,obj,load=fixture();load.References=[(obj,('A',))]
    nodes={n:tuple(v*scale for v in p) for n,p in nodes.items()}
    values=[float(v) for _,table in force(None,{},nodes,load) for v in table.values()]
    assert all(math.isfinite(v) for v in values)
    assert values==pytest.approx([0,0,0,40/3,40/3,40/3],abs=1e-12)
    assert math.fsum(values)==pytest.approx(40,abs=1e-12)


def test_unrepresentable_rescaled_weights_are_explicitly_rejected():
    force,nodes,_,obj,load=fixture();load.References=[(obj,('A',))]
    nodes={n:tuple(v*1e-161 for v in p) for n,p in nodes.items()}
    with pytest.raises(ValueError,match='R1_T6_UNREPRESENTABLE_WEIGHTS'):
        force(None,{},nodes,load)


def test_nonfinite_emitted_quantity_cannot_escape_validation():
    force,nodes,_,obj,load=fixture();load.References=[(obj,('A',))]
    force.__globals__['get_ref_facenodes_areas']=lambda nodes,table:list(zip(range(1,7),(1e308,-1e308,1.,0.,0.,0.)))
    with pytest.raises(ValueError,match='R1_NONFINITE_NODE_LOAD'):force(None,{},nodes,load)
