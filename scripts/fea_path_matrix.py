"""P0-only serialization diagnostic, never a full FEA acceptance."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--workspace',type=Path,required=True)
    args=parser.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from fea_native_probe import parse_config
    from fea_path_matrix import run_matrix
    report=run_matrix(parse_config(args.config),args.workspace)
    print(json.dumps({k:report[k] for k in ('diagnostic_status','p0_status','platform')}))
    return 0 if report['diagnostic_status']=='passed' else 3


if __name__=='__main__':raise SystemExit(main())
