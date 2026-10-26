"""Run approved R1 automated evidence; operator GUI gate remains independent."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--native-scratch-root',type=Path,required=True)
    args=parser.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from fea_native_probe import parse_config
    from fea_r1_runner import run_r1
    report=run_r1(parse_config(args.config),args.workspace,args.native_scratch_root)
    print(json.dumps({'automated_status':report['automated_status'],'status':report['status'],'report':str(args.workspace/'r1-report.json')}))
    return 0 if report['automated_status']=='passed' else 3


if __name__=='__main__':raise SystemExit(main())
