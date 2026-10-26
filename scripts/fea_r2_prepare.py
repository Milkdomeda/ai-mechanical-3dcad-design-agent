"""Explicit synthetic qualification setup; not an installed deployment entry point."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    for name in ('config','workspace','native-scratch-root','destination'):
        parser.add_argument('--'+name,type=Path,required=True)
    args = parser.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from test_analysis_geometry_live import native_settings
    from mechanical_design_agent.analysis_runtime import prepare_nodal_rf_runtime
    settings = native_settings(args.workspace,args.config,args.native_scratch_root)
    created = prepare_nodal_rf_runtime(settings,args.destination)
    print(json.dumps({'config':str(created),'qualification':'not_executed'}))


if __name__ == '__main__': main()
