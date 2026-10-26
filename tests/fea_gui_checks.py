"""Bounded P0 native-GUI readiness checks; standalone embedded-Python safe."""


VIEW_PROVIDERS = {
    'Material': ('App::MaterialObjectPython','femviewprovider.view_material_common','VPMaterialCommon'),
    'CalculiX': ('Fem::FemSolverObjectPython','femviewprovider.view_solver_ccxtools','VPSolverCcxTools'),
    'Mesh': ('Fem::FemMeshShapeBaseObjectPython','femviewprovider.view_mesh_gmsh','VPMeshGmsh'),
    'CCX_Results': ('Fem::FemResultObjectPython','femviewprovider.view_result_mechanical','VPResultMechanical'),
    'CCX_Results_Mesh': ('Fem::FemMeshObjectPython','femviewprovider.view_mesh_result','VPFemMeshResult'),
}


def assess_gui_objects(snapshot, expected_mesh, *, has_results):
    """A solver field bundle alone never establishes native GUI usability."""
    checks={}
    try:
        objects=snapshot['objects'];index={obj['name']:obj for obj in objects}
        checks['unique_objects']=len(index)==len(objects)
        names=('Material','CalculiX','Mesh')+(('CCX_Results','CCX_Results_Mesh') if has_results else ())
        checks['standard_view_providers']=all(
            tuple(index[name][key] for key in ('type_id','proxy_module','proxy_class'))==VIEW_PROVIDERS[name]
            for name in names
        )
        actual=snapshot['mesh_inventory']
        checks['mesh_inventory']=actual==expected_mesh and all(type(actual[k]) is int and actual[k]>0 for k in ('nodes','elements'))
        checks['solver_dialog']=snapshot['solver_dialog_open'] is True
        checks['result_presence']=snapshot['result_present'] is has_results
        if has_results:
            fields=snapshot['result_inventory']
            checks['result_fields']=fields['complete'] is True and type(fields['nodes']) is int and fields['nodes']==expected_mesh['nodes']
            checks['result_dialog']=snapshot['result_dialog_open'] is True
            checks['result_dialog_closed']=snapshot['result_dialog_closed'] is True
            checks['visible_result_mesh']=snapshot['visible_result_mesh'] is True
            contexts=snapshot['graphics_contexts']
            checks['opengl_context']=bool(contexts) and all(c['valid'] is True and type(c['version']) is list and len(c['version'])==2 and all(type(v) is int for v in c['version']) and c['version'][0]>=2 for c in contexts)
    except (KeyError,TypeError,ValueError):checks['complete_native_receipt']=False
    return {'status':'passed' if checks and all(checks.values()) else 'failed','checks':checks}


def inspect_generated_archive(path, expected_inventory):
    """Acceptance fixtures only. Audit exact native JSON-null proxy states.

    This does not grant permission to restore customer FEM documents and is
    deliberately separate from the product's strict CAD security boundary.
    """
    from pathlib import Path,PurePosixPath
    import stat
    import xml.etree.ElementTree as ET
    import zipfile
    document_proxies={
        'Material':('femobjects.material_common','MaterialCommon'),
        'CalculiX':('femobjects.solver_ccxtools','SolverCcxTools'),
        'Mesh':('femobjects.mesh_gmsh','MeshGmsh'),
        'CCX_Results':('femobjects.result_mechanical','ResultMechanical'),
        'CCX_Results_Mesh':('femobjects.mesh_result','MeshResult'),
    }
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>256*1024*1024:raise ValueError('fixture archive boundary')
    with zipfile.ZipFile(path) as archive:
        infos=archive.infolist();names=[v.filename for v in infos]
        if len(names)!=len(set(names)) or len(names)>1000:raise ValueError('fixture member inventory')
        if sum(v.file_size for v in infos)>256*1024*1024:raise ValueError('fixture archive size')
        for item in infos:
            name=item.filename
            if name.startswith(('/','\\')) or '\\' in name or ':' in name or '..' in PurePosixPath(name).parts or stat.S_ISLNK(item.external_attr>>16):raise ValueError('fixture archive path')
            if Path(name).suffix.lower() in {'.py','.pyc','.fcmacro','.exe','.dll','.dylib','.so','.bat','.cmd','.ps1','.sh'}:raise ValueError('fixture executable member')
        documents={}
        for name in ('Document.xml','GuiDocument.xml'):
            if name not in names:raise ValueError('missing fixture '+name)
            raw=archive.read(name)
            if b'<!doctype' in raw.lower() or b'<!entity' in raw.lower():raise ValueError('fixture XML declaration')
            documents[name]=ET.fromstring(raw)
    inventory=[{'name':v.get('name'),'type_id':v.get('type')} for v in documents['Document.xml'].findall('./Objects/Object')]
    if inventory!=expected_inventory or len({v['name'] for v in inventory})!=len(inventory):raise ValueError('fixture object inventory')
    expected_names={v['name'] for v in inventory}
    seen={};gui={}
    for filename,container,tag,allowed in (
        ('Document.xml','ObjectData','Object',document_proxies),
        ('GuiDocument.xml','ViewProviderData','ViewProvider',{k:v[1:] for k,v in VIEW_PROVIDERS.items()}),
    ):
        counted=0;found=set()
        for obj in documents[filename].findall('./'+container+'/'+tag):
            for prop in obj.findall('./Properties/Property'):
                for node in prop.iter('Python'):
                    counted+=1;name=obj.get('name')
                    if prop.get('name')!='Proxy' or name not in allowed or name not in expected_names or name in found:raise ValueError('fixture Proxy identity')
                    if (node.get('module'),node.get('class'))!=allowed[name] or node.get('encoded')!='yes' or node.get('value')!='bnVsbA==':raise ValueError('fixture unreviewed serialized Proxy')
                    found.add(name)
                    if filename=='GuiDocument.xml':gui[name]='.'.join(allowed[name])
        if counted!=len(list(documents[filename].iter('Python'))) or found!=(set(allowed)&expected_names):raise ValueError('fixture missing/unscoped Proxy')
        seen[filename]=sorted(found)
    return {'object_inventory':inventory,'gui_view_providers':gui,'proxy_inventory':seen}


def assess_display_fields(fields):
    """Compare UI quantities using native formatting resolution, not solver tolerances."""
    import math
    import re
    checks={}
    expected={'displacement_x':'mm','displacement_magnitude':'mm','von_mises':'MPa'}
    try:
        checks['field_inventory']=set(fields)==set(expected)
        for name,unit in expected.items():
            field=fields[name]
            good=field['unit']==unit and field['deformation_scale']==0 and field['legend_policy']=='freecad-1.1.3-native-nodal' and re.fullmatch('[0-9a-f]{64}',field['nodal_values_sha256']) is not None
            pixels=field['viewport_pixels']
            good=good and pixels['status']=='passed' and pixels['colored_pixels']>0 and pixels['colored_fraction']>=.01
            low,high=field['raw_min'],field['raw_max']
            good=good and all(type(v) in (int,float) and math.isfinite(v) for v in (low,high)) and low<=high
            for raw,key in ((low,'le_min'),(high,'le_max')):
                value=field[key]['normalized_value'];quantum=field[key]['display_quantum']
                if not all(type(v) in (int,float) and math.isfinite(v) for v in (value,quantum)) or quantum<=0:good=False;continue
                # Native ResultShow formats Stats to six significant digits,
                # then InputField rounds in the user's displayed unit.
                native_rounding=0.5*10**(math.floor(math.log10(abs(raw)))-5) if raw else 0.
                allowance=quantum/2+native_rounding+math.ulp(value)+math.ulp(raw)
                if abs(value-raw)>allowance:good=False
            checks[name]=bool(good)
    except (KeyError,TypeError,ValueError,OverflowError):checks['complete_display_receipt']=False
    return {'status':'passed' if checks and all(checks.values()) else 'failed','checks':checks,'comparison_basis':'native six-significant-digit formatting plus actual displayed-unit quantum; physical tolerances unchanged'}
