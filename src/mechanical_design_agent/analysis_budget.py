"""Cooperative wall/disk and observed-memory budgets, never an OS quota."""
import os
from pathlib import Path
import stat
import time

from .analysis_types import AnalysisError


def disk_bytes(roots):
    total=0;seen=set()
    for root in roots:
        if root is None:continue
        root=Path(root)
        if not root.exists():continue
        pending=[root]
        while pending:
            path=pending.pop()
            try:facts=path.lstat()
            except FileNotFoundError:continue  # native writers may rename temps
            if stat.S_ISLNK(facts.st_mode) or getattr(facts,'st_file_attributes',0)&0x400:raise AnalysisError('ANALYSIS_STORAGE_UNSAFE','budget roots cannot contain links or reparse points')
            identity=(facts.st_dev,facts.st_ino)
            if identity in seen:continue
            seen.add(identity)
            if stat.S_ISDIR(facts.st_mode):
                with os.scandir(path) as entries:pending.extend(Path(entry.path) for entry in entries)
            elif stat.S_ISREG(facts.st_mode):total+=facts.st_size
            else:raise AnalysisError('ANALYSIS_STORAGE_UNSAFE','analysis evidence must use regular files and directories')
    return total


class BudgetGuard:
    def __init__(self,budget,*,roots,cancelled,memory_observer,clock=None,integrity_checkpoint=None):
        if budget['memory_mode']!='observed_only':raise AnalysisError('ANALYSIS_MEMORY_MODE_UNSUPPORTED','this profile samples memory and stops on observed excess, without a hard OS reservation')
        self.budget,self.roots,self.cancelled,self.memory_observer=budget,roots,cancelled,memory_observer
        self.clock=clock or time.monotonic
        self.started=self.clock();self.next_probe=self.started
        self.integrity_checkpoint=integrity_checkpoint or (lambda:None)
        self.peak_memory_bytes=0;self.peak_disk_bytes=0;self.memory_measurement=None
        self.owned_roots=set()

    def track_root(self,path):self.owned_roots.add(Path(path))

    def release_root(self,path):self.owned_roots.discard(Path(path))

    def checkpoint(self,*,force=False):
        if self.cancelled():raise AnalysisError('ANALYSIS_CANCELLED','owned analysis cancellation was requested')
        now=self.clock()
        if now-self.started>=self.budget['wall_seconds']:raise AnalysisError('ANALYSIS_TIMED_OUT','analysis wall-clock budget exhausted')
        if not force and now<self.next_probe:return
        self.next_probe=now+.5
        self.integrity_checkpoint()
        try:observed=self.memory_observer()
        except Exception as exc:raise AnalysisError('ANALYSIS_MEMORY_UNAVAILABLE','owned memory observation failed; budget cannot be claimed') from exc
        if type(observed) is not dict or type(observed.get('bytes')) is not int or observed['bytes']<=0 or type(observed.get('measurement')) is not str:raise AnalysisError('ANALYSIS_MEMORY_UNAVAILABLE','owned memory observation is unavailable')
        self.memory_measurement=observed['measurement'];self.peak_memory_bytes=max(self.peak_memory_bytes,observed['bytes'])
        if observed['bytes']>self.budget['max_memory_mb']*1024*1024:raise AnalysisError('ANALYSIS_BUDGET_EXCEEDED','observed memory exceeds the declared budget')
        used=disk_bytes([*self.roots(),*self.owned_roots]);self.peak_disk_bytes=max(self.peak_disk_bytes,used)
        if used>self.budget['max_disk_bytes']:raise AnalysisError('ANALYSIS_BUDGET_EXCEEDED','actual current-run and owned W1 evidence exceeds the declared disk budget')

    def receipt(self):
        return {'mode':'observed_only','measurement':self.memory_measurement,'peak_observed_memory_bytes':self.peak_memory_bytes,'peak_observed_disk_bytes':self.peak_disk_bytes,'wall_seconds':self.clock()-self.started,'sample_interval_seconds':.5,'hard_os_quota':False}
