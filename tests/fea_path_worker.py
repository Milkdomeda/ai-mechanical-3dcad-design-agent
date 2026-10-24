"""Fixed P0 serialization diagnostic. New C++ objects only; never restore a file."""
from pathlib import Path
import json
import sys


def main(request_path, response_path):
    import FreeCAD as App
    import Fem
    import Part
    request=json.loads(Path(request_path).read_text(encoding='utf-8'))
    root=Path(request['external'])
    result={'version':'.'.join(App.Version()[:3]), 'temp_path':App.getTempPath(), 'auto_reopen_count':0}
    original=App.openDocument
    def deny(*args, **kwargs):
        result['auto_reopen_count']+=1
        raise RuntimeError('Persisted document restore forbidden')
    App.openDocument=deny
    documents=[]
    try:
        mesh=Fem.FemMesh()
        coordinates=((0,0,0),(2,0,0),(0,2,0),(0,0,2),(1,0,0),(1,1,0),(0,1,0),(0,0,1),(1,0,1),(0,1,1))
        for i,xyz in enumerate(coordinates,1):mesh.addNode(*xyz,i)
        mesh.addVolume(list(range(1,11)),1)
        result.update(mesh_nodes=mesh.NodeCount, mesh_volumes=mesh.VolumeCount)
        doc=App.newDocument('MeshDiagnostic');documents.append(doc.Name)
        obj=doc.addObject('Fem::FemMeshObject','Mesh');obj.FemMesh=mesh;doc.recompute()
        def attempt(operation):
            try:
                operation()
                return {'ok':True,'error':''}
            except Exception as exc:
                return {'ok':False,'error':str(exc),'exception':type(exc).__name__}
        result['mesh_save']=attempt(lambda:doc.saveAs(str(root/'mesh.FCStd')))
        result['direct_unv']=attempt(lambda:mesh.write(str(root/'direct.unv')))
        plain=App.newDocument('PlainDiagnostic');documents.append(plain.Name)
        part=plain.addObject('Part::Feature','Solid');part.Shape=Part.makeBox(1,1,1);plain.recompute()
        result['plain_save']=attempt(lambda:plain.saveAs(str(root/'plain.FCStd')))
    except Exception as exc:
        result['error']=type(exc).__name__+': '+str(exc)
    finally:
        for name in documents:App.closeDocument(name)
        App.openDocument=original
    Path(response_path).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main(*sys.argv[1:])
