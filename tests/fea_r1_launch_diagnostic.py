"""Bounded Windows identity-startup diagnostic; never creates CAD documents."""
import json
import os
from pathlib import Path
import sys
import time


def main():
    from mechanical_design_agent.analysis_process import ProcessController, ProcessRequest
    from fea_native_probe import console_statement
    from fea_r1_runtime import windows_path_key
    root, base_config = map(Path, sys.argv[1:])
    config = json.loads(base_config.read_text(encoding='utf-8'))
    original = Path(config['tools']['freecadcmd']['path'])
    executable = root / original.parent.parent.name / 'bin' / original.name
    expected = json.loads((root / 'identity/request.json').read_text(encoding='utf-8'))
    output = root / 'launch-diagnostic'
    output.mkdir()
    results = {}
    for label, extended_executable, extended_directory in (
        ('prefix_all', True, True), ('plain_executable', False, True),
        ('plain_directories', True, False), ('plain_all', False, False),
    ):
        directory = output / label
        directory.mkdir()
        plain = windows_path_key(str(directory.resolve()))
        native_directory = '\\\\?\\' + plain if extended_directory else plain
        tool = windows_path_key(str(executable.resolve()))
        if extended_executable:
            tool = '\\\\?\\' + tool
        request, response = directory / 'request.json', directory / 'response.json'
        request.write_text(json.dumps(expected), encoding='utf-8')
        env = {k:v for k,v in os.environ.items() if k in {'SYSTEMROOT','WINDIR','COMSPEC','PATHEXT'}}
        env.update(HOME=native_directory, USERPROFILE=native_directory,
                   FREECAD_USER_HOME=native_directory, FREECAD_USER_DATA=native_directory,
                   TMPDIR=native_directory, TEMP=native_directory, TMP=native_directory,
                   FREECAD_USER_TEMP=native_directory, PATH=os.defpath,
                   QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1')
        argv = (tool, '-u', native_directory+'/user.cfg', '-s', native_directory+'/system.cfg',
                '-c', console_statement([str(Path(__file__).with_name('fea_r1_identity.py')),
                                        native_directory+'/request.json', native_directory+'/response.json']))
        controller = ProcessController()
        process = controller.start(ProcessRequest(argv=argv,cwd=directory,env=env,
            stdout_path=directory/'stdout.log',stderr_path=directory/'stderr.log',timeout_seconds=45))
        try:
            while (outcome:=controller.poll(process)) is None:
                time.sleep(.05)
        finally:
            controller.terminate(process,reason='interrupted')
        value = json.loads(response.read_text(encoding='utf-8')) if response.is_file() else None
        results[label]={'reason':outcome.reason,'returncode':outcome.returncode,
                        'response':value,'success':value is not None and 'error' not in value}
    (output/'matrix.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps({k:{'success':v['success'],'reason':v['reason'],'returncode':v['returncode']} for k,v in results.items()}))


if __name__=='__main__':
    main()
