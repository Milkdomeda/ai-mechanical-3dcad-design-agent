"""P0 preparation/owned GUI/evidence CLI; never opens persisted CAD."""
import argparse
import json
from pathlib import Path
import sys


def main():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from fea_gui_evidence import prepare,read_json,validate_case,write_json,pin,checked,checked_session,summarize,write_report
    from fea_gui_session import serve
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('prepare');p.add_argument('--manifest',type=Path,required=True);p.add_argument('--workspace',type=Path,required=True)
    for name in ('launch','console','check','stop','summary'):
        p=commands.add_parser(name);p.add_argument('--session',type=Path,required=True)
        if name=='launch':
            p.add_argument('--config',type=Path,required=True);p.add_argument('--native-scratch-root',type=Path,required=True);p.add_argument('--timeout-seconds',type=int,default=7200)
        elif name=='console':p.add_argument('--case')
        elif name=='check':p.add_argument('--case',required=True);p.add_argument('--actions',type=Path,required=True)
        elif name=='summary':p.add_argument('--case-actions',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='prepare':
        print(json.dumps({'session':str(prepare(args.manifest,args.workspace,'macOS'))}));return 0
    session=checked_session(args.session);root=Path(session['workspace'])
    checked(read_json(root/'source-manifest.json')['session'])
    if args.command=='launch':
        print(json.dumps(serve(args.session,args.config,args.native_scratch_root,timeout_seconds=args.timeout_seconds)));return 0
    if args.command=='summary':
        mapping=read_json(args.case_actions)
        if not isinstance(mapping,dict):raise ValueError('case-to-actions mapping required')
        result=summarize(args.session,{key:Path(value) for key,value in mapping.items()})
        target=root/('p0-gui-acceptance-'+__import__('uuid').uuid4().hex+'.json')
        write_report(target,result);print(json.dumps(result,ensure_ascii=False));return 0 if result['macOS_gui_status']=='passed' else 3
    if args.command=='check':
        actions=read_json(args.actions)
        if not isinstance(actions,list):raise ValueError('explicit action list required')
        result=validate_case(args.session,args.case,actions)
        write_report(root/'comparison'/(args.case+'-'+__import__('uuid').uuid4().hex+'.json'),result)
        print(json.dumps(result,ensure_ascii=False));return 0 if result['status']=='passed' else 3
    if args.command=='stop':
        write_json(root/'stop.json',{'session_id':session['session_id'],'action':'stop'});return 0
    request=root/'receipts/identity-request.json'
    if args.case:
        if args.case not in {case['case_id'] for case in session['cases']}:raise ValueError('unknown case')
        launch=read_json(root/'receipts/launch.json');value=read_json(request)
        value.update(case_id=args.case,gui_pid=launch['gui_pid'],session=pin(args.session),response_path=str(root/'receipts'/(args.case+'-gui.json')))
        request=root/'receipts'/(args.case+'-request.json');write_json(request,value)
    worker=Path(__file__).resolve().parents[1]/'tests/fea_gui_identity.py'
    statement=f"import sys, runpy; sys.dont_write_bytecode=True; sys.path.insert(0,{str(worker.parent)!r}); sys.argv=[{str(worker)!r},{str(request)!r}]; runpy.run_path({str(worker)!r},run_name='__main__')"
    print(statement);return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,KeyError) as exc:
        print(json.dumps({'status':'failed','error':str(exc)}),file=sys.stderr);raise SystemExit(3)
