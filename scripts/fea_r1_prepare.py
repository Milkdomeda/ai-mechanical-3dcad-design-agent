"""Build an explicitly selected isolated native runtime; never an in-place patch."""
import argparse
import json
from pathlib import Path
import sys


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source-root',type=Path,required=True)
    p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--base-config',type=Path,required=True)
    p.add_argument('--source-archive-sha256')
    p.add_argument('--baseline',action='store_true')
    args=p.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from fea_native_probe import parse_config
    from fea_r1_runtime import prepare_runtime
    config=prepare_runtime(args.source_root,args.destination,parse_config(args.base_config),source_archive_sha256=args.source_archive_sha256,baseline=args.baseline)
    print(json.dumps({'config':str(config),'baseline':args.baseline}))


if __name__=='__main__':main()
