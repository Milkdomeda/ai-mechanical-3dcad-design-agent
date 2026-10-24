"""Windows: suspended creation, worker-owned kill-on-close Job Object."""
from __future__ import annotations

import os
import subprocess


class Child:
    def __init__(self, process, pid, job):
        self.process = process
        self.pid = pid
        self.job = job

    def poll(self):
        import win32event
        import win32process
        if win32event.WaitForSingleObject(self.process, 0) == 258:
            return None
        return win32process.GetExitCodeProcess(self.process)

    def terminate(self):
        import win32event
        import win32job
        import win32process
        win32job.TerminateJobObject(self.job, 1)
        if win32event.WaitForSingleObject(self.process, 4000) == 258:
            raise RuntimeError("Windows owned job did not terminate")
        return win32process.GetExitCodeProcess(self.process)

    def close(self):
        self.job.Close()
        self.process.Close()


def start(request, token, stdout, stderr):
    import msvcrt
    import win32con
    import win32job
    import win32process
    job = win32job.CreateJobObject(None, "")
    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    process = None
    thread = None
    null = open(os.devnull, "rb")
    streams = [null, stdout, stderr]
    handles = [msvcrt.get_osfhandle(stream.fileno()) for stream in streams]
    try:
        for handle in handles:
            os.set_handle_inheritable(handle, True)
        startup = win32process.STARTUPINFO()
        startup.dwFlags |= win32con.STARTF_USESTDHANDLES
        startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
        process, thread, pid, _ = win32process.CreateProcess(None, subprocess.list2cmdline(request.argv), None, None, True, win32process.CREATE_SUSPENDED | win32process.CREATE_NEW_PROCESS_GROUP | win32process.CREATE_UNICODE_ENVIRONMENT, dict(request.env), str(request.cwd), startup)
        win32job.AssignProcessToJobObject(job, process)
        win32process.ResumeThread(thread)
        return Child(process, pid, job)
    except BaseException:
        if process:
            win32process.TerminateProcess(process, 1)
            process.Close()
        job.Close()
        raise
    finally:
        if thread:
            thread.Close()
        for handle in handles:
            os.set_handle_inheritable(handle, False)
        null.close()
