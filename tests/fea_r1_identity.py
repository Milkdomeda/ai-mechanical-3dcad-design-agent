"""Native identity only, never opens or creates a CAD document."""
import sys
sys.dont_write_bytecode=True
import json
from pathlib import Path
import traceback
sys.path.insert(0,str(Path(__file__).parent))
from fea_r1_runtime import loaded_module_receipt,native_identity,startup_receipt


def main():
    request,response=map(Path,sys.argv[1:])
    try:
        from femmesh import meshtools
        expected=json.loads(request.read_text(encoding='utf-8'))
        result={'identity':native_identity(),'module':loaded_module_receipt(expected,meshtools),'startup':startup_receipt(request.parent)}
    except Exception:result={'error':traceback.format_exc()}
    response.write_text(json.dumps(result,indent=2),encoding='utf-8')


if __name__=='__main__':main()
