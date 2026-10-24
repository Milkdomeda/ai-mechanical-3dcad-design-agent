"""Run fixed synthetic native GUI checks; this is P0 infrastructure only."""
import argparse
import json
from pathlib import Path
import sys


def result_summary(workspace,result):
    # ASCII transport also works with Windows consoles using legacy code pages.
    return json.dumps({'report':str(workspace/'gui-automation.json'),'automated_status':result['automated_status'],'visual_review_status':result['visual_review_status'],'overall_p0_status':result['overall_p0_status'],'error':result.get('error')},ensure_ascii=True)


def main():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from fea_gui_native import CASE_IDS
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--native-scratch-root',type=Path,required=True)
    parser.add_argument('--case',action='append',choices=CASE_IDS)
    parser.add_argument('--software-opengl',action='store_true',help='Explicitly request the official distribution\'s bundled software OpenGL; no runtime files are changed.')
    parser.add_argument('--gui-environment',type=Path,help='Explicit manifest from isolated Windows GUI materialization.')
    args=parser.parse_args()
    from fea_native_probe import parse_config
    from fea_gui_automation import run_gui_automation
    result=run_gui_automation(parse_config(args.config),args.workspace,args.native_scratch_root,case_ids=tuple(args.case or CASE_IDS),software_opengl=args.software_opengl,gui_environment=args.gui_environment)
    print(result_summary(args.workspace,result))
    return 0 if result['automated_status']=='passed' else 3


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,KeyError) as exc:
        print(json.dumps({'status':'failed','error':str(exc)}),file=sys.stderr);raise SystemExit(3)
