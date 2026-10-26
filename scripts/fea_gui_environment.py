"""Materialize the isolated official Windows GUI compatibility environment."""
import argparse
import json
from pathlib import Path
import sys


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from fea_gui_environment import prepare_environment
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--native-scratch-root',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    print(json.dumps({'environment':str(prepare_environment(args.config,args.native_scratch_root,args.output))},ensure_ascii=True))
