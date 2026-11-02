"""LPAC process launcher. This helper never evaluates model-provided Python."""
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


class StartupInfo(C.Structure):
    _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR), ("lpTitle", W.LPWSTR)] + [
        (name, W.DWORD) for name in ("dwX", "dwY", "dwXSize", "dwYSize", "dwXCountChars", "dwYCountChars", "dwFillAttribute", "dwFlags")
    ] + [("wShowWindow", W.WORD), ("cbReserved2", W.WORD), ("lpReserved2", C.c_void_p),
         ("hStdInput", W.HANDLE), ("hStdOutput", W.HANDLE), ("hStdError", W.HANDLE)]


class StartupEx(C.Structure):
    _fields_ = [("info", StartupInfo), ("attributes", C.c_void_p)]


class ProcessInfo(C.Structure):
    _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE), ("pid", W.DWORD), ("tid", W.DWORD)]


class AclSizeInfo(C.Structure):
    _fields_ = [("count", W.DWORD), ("used", W.DWORD), ("free", W.DWORD)]


class AceHeader(C.Structure):
    _fields_ = [("kind", W.BYTE), ("flags", W.BYTE), ("size", W.WORD)]


class AllowedAce(C.Structure):
    _fields_ = [("header", AceHeader), ("mask", W.DWORD), ("sid_start", W.DWORD)]


class Capabilities(C.Structure):
    _fields_ = [("sid", C.c_void_p), ("capabilities", C.c_void_p), ("count", W.DWORD), ("reserved", W.DWORD)]


class Trustee(C.Structure):
    _fields_ = [("multiple", C.c_void_p), ("operation", W.DWORD), ("form", W.DWORD), ("kind", W.DWORD), ("name", C.c_void_p)]


class Access(C.Structure):
    _fields_ = [("permissions", W.DWORD), ("mode", W.DWORD), ("inheritance", W.DWORD), ("trustee", Trustee)]


class SidAttributes(C.Structure):
    _fields_ = [("sid", C.c_void_p), ("attributes", W.DWORD)]


class BasicLimits(C.Structure):
    _fields_ = [("process_time", C.c_longlong), ("job_time", C.c_longlong), ("flags", W.DWORD),
        ("minimum_ws", C.c_size_t), ("maximum_ws", C.c_size_t), ("active_processes", W.DWORD),
        ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]


class JobLimits(C.Structure):
    _fields_ = [("basic", BasicLimits), ("io", C.c_ulonglong * 6), ("process_memory", C.c_size_t),
        ("job_memory", C.c_size_t), ("peak_process_memory", C.c_size_t), ("peak_job_memory", C.c_size_t)]


class WindowsAPI:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows AppContainer is unavailable on this platform")
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.userenv = C.WinDLL("userenv", use_last_error=True)
        self.advapi = C.WinDLL("advapi32", use_last_error=True)
        self.base = C.WinDLL("kernelbase", use_last_error=True)
        declarations = [
            (self.base, "DeriveCapabilitySidsFromName", [W.LPCWSTR, C.POINTER(C.c_void_p), C.POINTER(W.DWORD), C.POINTER(C.c_void_p), C.POINTER(W.DWORD)], W.BOOL),
            (self.userenv, "CreateAppContainerProfile", [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)], C.c_long),
            (self.userenv, "DeleteAppContainerProfile", [W.LPCWSTR], C.c_long),
            (self.userenv, "DeriveAppContainerSidFromAppContainerName", [W.LPCWSTR, C.POINTER(C.c_void_p)], C.c_long),
            (self.advapi, "ConvertSidToStringSidW", [C.c_void_p, C.POINTER(W.LPWSTR)], W.BOOL),
            (self.advapi, "FreeSid", [C.c_void_p], C.c_void_p),
            (self.advapi, "GetFileSecurityW", [W.LPCWSTR, W.DWORD, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
            (self.advapi, "GetSecurityDescriptorDacl", [C.c_void_p, C.POINTER(W.BOOL), C.POINTER(C.c_void_p), C.POINTER(W.BOOL)], W.BOOL),
            (self.advapi, "GetAclInformation", [C.c_void_p, C.c_void_p, W.DWORD, W.DWORD], W.BOOL),
            (self.advapi, "GetAce", [C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)], W.BOOL),
            (self.advapi, "EqualSid", [C.c_void_p, C.c_void_p], W.BOOL),
            (self.advapi, "SetEntriesInAclW", [W.DWORD, C.POINTER(Access), C.c_void_p, C.POINTER(C.c_void_p)], W.DWORD),
            (self.advapi, "InitializeSecurityDescriptor", [C.c_void_p, W.DWORD], W.BOOL),
            (self.advapi, "SetSecurityDescriptorDacl", [C.c_void_p, W.BOOL, C.c_void_p, W.BOOL], W.BOOL),
            (self.advapi, "SetFileSecurityW", [W.LPCWSTR, W.DWORD, C.c_void_p], W.BOOL),
            (self.kernel, "LocalFree", [C.c_void_p], C.c_void_p),
            (self.kernel, "InitializeProcThreadAttributeList", [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)], W.BOOL),
            (self.kernel, "UpdateProcThreadAttribute", [C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p], W.BOOL),
            (self.kernel, "DeleteProcThreadAttributeList", [C.c_void_p], None),
            (self.kernel, "CreateProcessW", [W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD, C.c_void_p, W.LPCWSTR, C.POINTER(StartupEx), C.POINTER(ProcessInfo)], W.BOOL),
            (self.kernel, "GetStdHandle", [W.DWORD], W.HANDLE),
            (self.kernel, "SetHandleInformation", [W.HANDLE, W.DWORD, W.DWORD], W.BOOL),
            (self.kernel, "ResumeThread", [W.HANDLE], W.DWORD),
            (self.kernel, "OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (self.kernel, "OpenEventW", [W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
            (self.kernel, "CreateEventW", [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
            (self.kernel, "SetEvent", [W.HANDLE], W.BOOL),
            (self.kernel, "TerminateProcess", [W.HANDLE, W.UINT], W.BOOL),
            (self.kernel, "CreateJobObjectW", [C.c_void_p, W.LPCWSTR], W.HANDLE),
            (self.kernel, "SetInformationJobObject", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
            (self.kernel, "AssignProcessToJobObject", [W.HANDLE, W.HANDLE], W.BOOL),
            (self.kernel, "TerminateJobObject", [W.HANDLE, W.UINT], W.BOOL),
            (self.kernel, "WaitForMultipleObjects", [W.DWORD, C.POINTER(W.HANDLE), W.BOOL, W.DWORD], W.DWORD),
            (self.kernel, "WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
            (self.kernel, "GetExitCodeProcess", [W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
            (self.kernel, "CloseHandle", [W.HANDLE], W.BOOL),
        ]
        for library, name, arguments, result in declarations:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def check(value):
        if not value:
            raise C.WinError(C.get_last_error())
        return value

    def traverse(self, path, sid, remove=False):
        """Non-inheriting directory traversal ACE; never enumerate ancestor trees."""
        size = W.DWORD()
        self.advapi.GetFileSecurityW(str(path), 4, None, 0, C.byref(size))
        descriptor = C.create_string_buffer(size.value)
        self.check(self.advapi.GetFileSecurityW(str(path), 4, descriptor, size, C.byref(size)))
        present, defaulted, old = W.BOOL(), W.BOOL(), C.c_void_p()
        self.check(self.advapi.GetSecurityDescriptorDacl(descriptor, C.byref(present), C.byref(old), C.byref(defaulted)))
        access = Access(0x20, 4 if remove else 1, 0, Trustee(None, 0, 0, 0, sid))
        updated = C.c_void_p()
        code = self.advapi.SetEntriesInAclW(1, C.byref(access), old, C.byref(updated))
        if code:
            raise C.WinError(code)
        try:
            target = C.create_string_buffer(64)
            self.check(self.advapi.InitializeSecurityDescriptor(target, 1))
            self.check(self.advapi.SetSecurityDescriptorDacl(target, True, updated, False))
            self.check(self.advapi.SetFileSecurityW(str(path), 4, target))
        finally:
            self.kernel.LocalFree(updated)

    def owns_writable_directory(self, path, sid):
        """Verify a legacy scratch directory's explicit Modify grant, not its name."""
        size = W.DWORD()
        self.advapi.GetFileSecurityW(str(path), 4, None, 0, C.byref(size))
        descriptor = C.create_string_buffer(size.value)
        self.check(self.advapi.GetFileSecurityW(str(path), 4, descriptor, size, C.byref(size)))
        present, defaulted, acl = W.BOOL(), W.BOOL(), C.c_void_p()
        self.check(self.advapi.GetSecurityDescriptorDacl(descriptor, C.byref(present), C.byref(acl), C.byref(defaulted)))
        if not present.value or not acl.value:
            return False
        info = AclSizeInfo()
        self.check(self.advapi.GetAclInformation(acl, C.byref(info), C.sizeof(info), 2))
        for index in range(info.count):
            entry = C.c_void_p()
            self.check(self.advapi.GetAce(acl, index, C.byref(entry)))
            header = C.cast(entry, C.POINTER(AceHeader)).contents
            # Allow ACE, not inherited/inherit-only; require write-data+delete.
            if header.size < 16 or header.kind != 0 or header.flags & 0x18:
                continue
            ace = C.cast(entry, C.POINTER(AllowedAce)).contents
            if ace.mask & 0x10002 != 0x10002:
                continue
            address = entry.value + AllowedAce.sid_start.offset
            length = 8 + 4 * C.c_ubyte.from_address(address + 1).value
            if AllowedAce.sid_start.offset + length <= header.size and self.advapi.EqualSid(sid, C.c_void_p(address)):
                return True
        return False


def restricted_environment(scratch):
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "OS", "COMSPEC", "PATHEXT", "PATH", "PROGRAMDATA",
        "ALLUSERSPROFILE", "PUBLIC", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
        "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH"}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update(TEMP=str(scratch), TMP=str(scratch), PYTHONUTF8="1", PYTHONIOENCODING="utf-8", MPLBACKEND="Agg")
    return result


def launch(spec):
    api = WindowsAPI()
    kernel = api.kernel
    identity = uuid.uuid4().hex
    name = "xuloom.sandbox." + identity
    sid, attributes, job, owner, event = C.c_void_p(), None, None, None, None
    process = ProcessInfo()
    capability_allocations, sid_text, scratch = [], "", None
    workspace = Path(spec["workspace"]).resolve(strict=True)
    cwd = Path(spec["cwd"]).resolve(strict=True)
    if not cwd.is_relative_to(workspace):
        raise ValueError("Sandbox cwd is outside its authorized workspace")
    from xuloom.security.sandbox_cleanup import directory, save, remove_grants, report_path
    from xuloom.core.session_lock import SessionLock
    cleanup_lock = SessionLock(directory(), identity)
    cleanup_lock.acquire()
    report = dict(id=identity, profile=name, workspace=str(workspace), status="active", sid=None,
                  grants=[], traversed=[], scratch=None, failures=[])
    icacls = str(Path(os.environ["SystemRoot"]) / "System32/icacls.exe")
    def acl(path, permission):
        if event and kernel.WaitForSingleObject(event, 0) == 0:
            raise InterruptedError("Sandbox setup cancelled")
        path = Path(path).resolve(strict=True)
        report["grants"].append(str(path)); save(report)
        completed = subprocess.run([icacls, str(path), "/L", "/grant", "*" + sid_text + ":" + permission],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if completed.returncode:
            raise OSError("Sandbox ACL setup failed; execution was not started")
    try:
        save(report)
        hr = api.userenv.CreateAppContainerProfile(name, name, "Xuloom restricted tool execution", None, 0, C.byref(sid))
        if hr < 0:
            raise OSError("AppContainer creation failed: " + hex(hr & 0xffffffff))
        text = W.LPWSTR()
        api.check(api.advapi.ConvertSidToStringSidW(sid, C.byref(text)))
        sid_text = text.value
        report["sid"] = sid_text; save(report)
        kernel.LocalFree(text)
        scratch = tempfile.mkdtemp(prefix="xuloom-restricted-" + identity + "-")
        details = os.lstat(scratch)
        report.update(scratch=scratch, scratch_identity=[details.st_dev, details.st_ino]); save(report)
        if scratch:
            event = api.check(kernel.OpenEventW(0x100000, False, spec["cancel_event"]))
            acl(workspace, "(OI)(CI)M")
            acl(scratch, "(OI)(CI)M")
            for root in spec["read_roots"]:
                path = Path(root).resolve(strict=True)
                acl(path, "(OI)(CI)RX" if path.is_dir() else "RX")
            for parent in spec.get("read_parents", []):
                acl(parent, "RX")
            for parent in set(p for item in [workspace] + [Path(root) for root in spec["read_roots"]] for p in item.parents):
                if parent.is_relative_to(Path.home()):
                    report["traversed"].append(str(parent)); save(report)
                    api.traverse(parent, sid)
            count = C.c_size_t()
            kernel.InitializeProcThreadAttributeList(None, 3, 0, C.byref(count))
            attributes = C.create_string_buffer(count.value)
            api.check(kernel.InitializeProcThreadAttributeList(attributes, 3, 0, C.byref(count)))
            groups, caps, group_count, cap_count = C.c_void_p(), C.c_void_p(), W.DWORD(), W.DWORD()
            api.check(api.base.DeriveCapabilitySidsFromName("registryRead", C.byref(groups), C.byref(group_count), C.byref(caps), C.byref(cap_count)))
            group_sids = C.cast(groups, C.POINTER(C.c_void_p))
            cap_sids = C.cast(caps, C.POINTER(C.c_void_p))
            capability_allocations.extend([group_sids[i] for i in range(group_count.value)] + [cap_sids[i] for i in range(cap_count.value)] + [groups, caps])
            enabled_caps = (SidAttributes * cap_count.value)(*[SidAttributes(cap_sids[i], 4) for i in range(cap_count.value)])
            capabilities, opt_out = Capabilities(sid, C.cast(enabled_caps, C.c_void_p), cap_count.value, 0), W.DWORD(1)
            handles = (W.HANDLE * 3)(*[kernel.GetStdHandle(value & 0xffffffff) for value in (-10, -11, -12)])
            for handle in handles:
                api.check(kernel.SetHandleInformation(handle, 1, 1))
            for key, value in ((0x20009, capabilities), (0x2000F, opt_out), (0x20002, handles)):
                api.check(kernel.UpdateProcThreadAttribute(attributes, 0, key, C.byref(value), C.sizeof(value), None, None))
            info = StartupEx()
            info.info.cb, info.info.dwFlags = C.sizeof(StartupEx), 0x101
            info.info.hStdInput, info.info.hStdOutput, info.info.hStdError = handles
            info.attributes = C.cast(attributes, C.c_void_p)
            environment = restricted_environment(scratch)
            block = C.create_unicode_buffer("\0".join(key + "=" + value for key, value in sorted(environment.items(), key=lambda p: p[0].upper())) + "\0\0")
            argv = spec["argv"]
            if not argv or any(not isinstance(value, str) or "\0" in value for value in argv):
                raise ValueError("Invalid sandbox argv")
            line = C.create_unicode_buffer(subprocess.list2cmdline(argv))
            if spec.get("shell_command") is not None:
                line = C.create_unicode_buffer(subprocess.list2cmdline(argv[:4]) + ' "' + spec["shell_command"] + '"')
            job = api.check(kernel.CreateJobObjectW(None, None))
            limits = JobLimits()
            limits.basic.flags, limits.basic.active_processes = 0x2000 | 0x8 | 0x200, 64
            limits.job_memory = 4 * 1024 * 1024 * 1024
            api.check(kernel.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)))
            api.check(kernel.CreateProcessW(argv[0], line, None, None, True, 0x80000 | 0x400 | 0x8000000 | 0x4,
                C.cast(block, C.c_void_p), str(cwd), C.byref(info), C.byref(process)))
            api.check(kernel.AssignProcessToJobObject(job, process.process))
            owner = api.check(kernel.OpenProcess(0x100000, False, spec["owner_pid"]))
            if kernel.ResumeThread(process.thread) == 0xffffffff:
                raise C.WinError(C.get_last_error())
            wait_handles = (W.HANDLE * 3)(process.process, owner, event)
            wait = kernel.WaitForMultipleObjects(3, wait_handles, False, spec.get("timeout_ms", 120000))
            if wait != 0:
                kernel.TerminateJobObject(job, 124 if wait == 258 else 130)
                kernel.WaitForSingleObject(process.process, 10000)
            code = W.DWORD()
            api.check(kernel.GetExitCodeProcess(process.process, C.byref(code)))
            return int(code.value)
    finally:
        if job:
            kernel.CloseHandle(job)  # KILL_ON_JOB_CLOSE also removes descendants.
        if process.process:
            code = W.DWORD()
            if kernel.GetExitCodeProcess(process.process, C.byref(code)) and code.value == 259:
                kernel.TerminateProcess(process.process, 126)
                kernel.WaitForSingleObject(process.process, 10000)
        for handle in (process.thread, process.process, owner, event):
            if handle:
                kernel.CloseHandle(handle)
        if attributes is not None:
            kernel.DeleteProcThreadAttributeList(attributes)
        try:
            remove_grants(report, api, icacls)
            if report["status"] != "cleaned":
                print("Sandbox execution ended; cleanup needs attention. Record: " + str(report_path(identity)), file=sys.stderr)
        finally:
            cleanup_lock.close()
            if sid.value:
                api.advapi.FreeSid(sid)
            for allocation in capability_allocations:
                kernel.LocalFree(allocation)


if __name__ == "__main__":
    try:
        spec_path = Path(sys.argv[1])
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        raise SystemExit(launch(spec))
    except InterruptedError:
        raise SystemExit(130)
    except Exception as error:
        print("System sandbox failed closed: " + str(error), file=sys.stderr)
        raise SystemExit(126)
