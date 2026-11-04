"""Standalone native process primitives reused from the preserved Harness.

Copied BasicLimits through WindowsAPI from crawler_harness/containment.py.
No JobStore, access registry or Agent dependency; original source untouched.
"""
from contextlib import contextmanager
import ctypes as c
from ctypes import wintypes as w
from dataclasses import dataclass
import os
import subprocess

class ContainmentError(RuntimeError):
    pass


class BasicLimits(c.Structure):
    _fields_ = [('process_time',c.c_int64),('job_time',c.c_int64),('flags',w.DWORD),
        ('minimum',c.c_size_t),('maximum',c.c_size_t),('active',w.DWORD),
        ('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]


class ExtendedLimits(c.Structure):
    _fields_ = [('basic',BasicLimits),('io',c.c_uint64*6),('memory',c.c_size_t*4)]


class Accounting(c.Structure):
    _fields_ = [('times',c.c_int64*4),('faults',w.DWORD),('total',w.DWORD),
                ('active',w.DWORD),('terminated',w.DWORD)]


class Startup(c.Structure):
    _fields_ = [('cb',w.DWORD),('reserved',w.LPWSTR),('desktop',w.LPWSTR),
        ('title',w.LPWSTR),('x',w.DWORD),('y',w.DWORD),('width',w.DWORD),
        ('height',w.DWORD),('xchars',w.DWORD),('ychars',w.DWORD),('fill',w.DWORD),
        ('flags',w.DWORD),('show',w.WORD),('reserved_size',w.WORD),
        ('reserved_bytes',c.c_void_p),('stdin',w.HANDLE),('stdout',w.HANDLE),('stderr',w.HANDLE)]


class StartupEx(c.Structure):
    _fields_ = [('startup',Startup),('attributes',c.c_void_p)]


class ProcessInfo(c.Structure):
    _fields_ = [('process',w.HANDLE),('thread',w.HANDLE),('pid',w.DWORD),('tid',w.DWORD)]


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation_filetime: int
    windows_session_id: int


class WindowsAPI:
    def __init__(self):
        if os.name != 'nt':
            raise ContainmentError('Windows 10+ Job Object creation support required')
        k = self.k = c.WinDLL('kernel32',use_last_error=True)
        p = c.c_void_p
        definitions = [
            ('CreateJobObjectW',[p,w.LPCWSTR],w.HANDLE),
            ('SetInformationJobObject',[w.HANDLE,c.c_int,p,w.DWORD],w.BOOL),
            ('QueryInformationJobObject',[w.HANDLE,c.c_int,p,w.DWORD,p],w.BOOL),
            ('InitializeProcThreadAttributeList',[p,w.DWORD,w.DWORD,c.POINTER(c.c_size_t)],w.BOOL),
            ('UpdateProcThreadAttribute',[p,w.DWORD,c.c_size_t,p,c.c_size_t,p,p],w.BOOL),
            ('DeleteProcThreadAttributeList',[p],None),
            ('CreateProcessW',[w.LPCWSTR,w.LPWSTR,p,p,w.BOOL,w.DWORD,p,w.LPCWSTR,p,c.POINTER(ProcessInfo)],w.BOOL),
            ('IsProcessInJob',[w.HANDLE,w.HANDLE,c.POINTER(w.BOOL)],w.BOOL),
            ('GetProcessTimes',[w.HANDLE,*([c.POINTER(w.FILETIME)]*4)],w.BOOL),
            ('ProcessIdToSessionId',[w.DWORD,c.POINTER(w.DWORD)],w.BOOL),
            ('GetProcessId',[w.HANDLE],w.DWORD),
            ('GetHandleInformation',[w.HANDLE,c.POINTER(w.DWORD)],w.BOOL),
            ('OpenProcess',[w.DWORD,w.BOOL,w.DWORD],w.HANDLE),
            ('WaitForSingleObject',[w.HANDLE,w.DWORD],w.DWORD),
            ('GetExitCodeProcess',[w.HANDLE,c.POINTER(w.DWORD)],w.BOOL),
            ('TerminateJobObject',[w.HANDLE,w.UINT],w.BOOL),
            ('ResumeThread',[w.HANDLE],w.DWORD),
            ('CloseHandle',[w.HANDLE],w.BOOL),
        ]
        for name,args,result in definitions:
            f=getattr(k,name);f.argtypes=args;f.restype=result

    @staticmethod
    def require(ok):
        if not ok:
            raise ContainmentError('Windows containment API failure: '+str(c.get_last_error()))

    def wait(self,handle,ms=0):
        value=self.k.WaitForSingleObject(handle,ms)
        if value not in (0,0x102):
            raise ContainmentError('Process handle wait failed')
        return value==0

    def identity(self,handle):
        values=[w.FILETIME() for _ in range(4)]
        self.require(self.k.GetProcessTimes(handle,*[c.byref(v) for v in values]))
        pid=self.k.GetProcessId(handle);self.require(pid)
        sid=w.DWORD();self.require(self.k.ProcessIdToSessionId(pid,c.byref(sid)))
        return ProcessIdentity(pid,(values[0].dwHighDateTime<<32)|values[0].dwLowDateTime,sid.value)

    def noninherited(self,handle):
        flags=w.DWORD();self.require(self.k.GetHandleInformation(handle,c.byref(flags)))
        if flags.value & 1:raise ContainmentError('Inheritable ownership handle refused')

    def member(self,process,job):
        yes=w.BOOL();self.require(self.k.IsProcessInJob(process,job,c.byref(yes)))
        if not yes.value:raise ContainmentError('Process not in owned Job Object')

    def active(self,job):
        value=Accounting()
        self.require(self.k.QueryInformationJobObject(job,1,c.byref(value),c.sizeof(value),None))
        return value.active

    def pids(self,job):
        capacity=16
        for _ in range(9):
            buf=c.create_string_buffer(8+capacity*c.sizeof(c.c_size_t))
            ok=self.k.QueryInformationJobObject(job,3,buf,c.sizeof(buf),None)
            if not ok and c.get_last_error()!=234:self.require(ok)
            assigned,listed=(w.DWORD*2).from_buffer(buf)
            if ok and assigned==listed:
                return list((c.c_size_t*listed).from_buffer(buf,8))
            capacity=max(capacity*2,assigned)
        raise ContainmentError('Descendant inventory exceeds bounded query')

    @contextmanager
    def attributes(self,job):
        size=c.c_size_t()
        self.k.InitializeProcThreadAttributeList(None,1,0,c.byref(size))
        if not size.value:raise ContainmentError('Job-list attributes unavailable')
        storage=c.create_string_buffer(size.value)
        self.require(self.k.InitializeProcThreadAttributeList(storage,1,0,c.byref(size)))
        try:
            jobs=(w.HANDLE*1)(job)
            self.require(self.k.UpdateProcThreadAttribute(storage,0,0x2000D,
                c.cast(jobs,c.c_void_p),c.sizeof(jobs),None,None))
            yield c.cast(storage,c.c_void_p)
        finally:self.k.DeleteProcThreadAttributeList(storage)

    def create(self,job,argv,cwd):
        info=ProcessInfo()
        with self.attributes(job) as attrs:
            startup=StartupEx();startup.startup.cb=c.sizeof(startup);startup.attributes=attrs
            command=c.create_unicode_buffer(subprocess.list2cmdline(argv))
            # CREATE_SUSPENDED is for pre-execution verification only. Membership
            # is atomic via JOB_LIST, never a later AssignProcessToJobObject.
            self.require(self.k.CreateProcessW(argv[0],command,None,None,False,
                0x80000|0x08000000|4,None,str(cwd),c.byref(startup),c.byref(info)))
        return info
