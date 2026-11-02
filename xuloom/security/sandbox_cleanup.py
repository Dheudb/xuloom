"""Durable, explicitly retryable cleanup of this launcher's own Windows grants."""
import ctypes
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import shutil
import tempfile
import time

from xuloom.core.session_lock import SessionLock
from xuloom.core.storage import IOPath, atomic_json, data_directory


def directory():
    return IOPath(data_directory() / "sandbox-cleanup")


def same_workspace(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def report_path(identity):
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise ValueError("Invalid sandbox cleanup identity")
    return directory() / (identity + ".json")


def save(report):
    report["updated_at"] = time.time()
    path = report_path(report["id"])
    if report.get("status") == "cleaned":
        atomic_json(directory() / "completed" / path.name, report)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    else:
        atomic_json(path, report)


def pending(workspace):
    output = []
    for path in directory().glob("*.json"):
        if not re.fullmatch(r"[a-f0-9]{32}",path.stem):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value,dict) and isinstance(value.get("workspace"),str) and not same_workspace(value["workspace"],workspace):
                continue
            validate_record(value)
            if value["id"] != path.stem:
                raise ValueError("Cleanup record identity does not match its file")
        except FileNotFoundError:
            continue  # A just-completed cleanup may have archived this record.
        except (OSError,ValueError,TypeError) as error:
            output.append({"id":path.stem,"status":"record_invalid","failures":[],"report_path":str(path),"error":str(error)[:1000]})
            continue
        if value.get("status") != "cleaned":
            held = SessionLock(directory(), value["id"]).locked()
            if held is not False:
                continue  # Never clean or call a live/unknown launch interrupted.
            output.append({"id": value["id"], "status": value.get("status"), "failures": value.get("failures", []),
                           "report_path": str(path)})
    return output


def validate_record(report):
    if not isinstance(report, dict):
        raise ValueError("Invalid sandbox cleanup record")
    report_path(report.get("id"))
    if report.get("profile") != "xuloom.sandbox." + report["id"]:
        raise ValueError("Invalid owned sandbox profile")
    for key in ("grants", "traversed"):
        if not isinstance(report.get(key), list) or not all(isinstance(p,str) and '\x00' not in p and Path(p).is_absolute() for p in report[key]):
            raise ValueError("Invalid sandbox cleanup paths")
    for key in ("workspace", "scratch"):
        value = report.get(key)
        if key == "scratch" and value is None:
            continue
        if not isinstance(value,str) or '\x00' in value or not Path(value).is_absolute():
            raise ValueError("Invalid sandbox cleanup path")
    identity = report.get("scratch_identity")
    if identity is not None and (report.get("scratch") is None or not isinstance(identity,list) or len(identity)!=2 or any(type(n) is not int or n<0 for n in identity)):
        raise ValueError("Invalid sandbox scratch identity")


def scratch_identity(path):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Sandbox scratch is not an ordinary directory")
    return [info.st_dev, info.st_ino]


def owned_scratch(report, api, sid):
    value = report.get("scratch")
    if not value or not os.path.lexists(value):
        return None
    raw = Path(value)
    actual = scratch_identity(raw)
    path = raw.resolve()
    workspace = Path(report["workspace"]).resolve()
    if (path.parent != Path(tempfile.gettempdir()).resolve() or not path.name.startswith("xuloom-restricted-") or
            path.is_relative_to(workspace) or workspace.is_relative_to(path)):
        raise ValueError("Scratch cleanup path is outside its owned temporary directory")
    expected = report.get("scratch_identity")
    if expected is not None:
        if not actual[1] or actual != expected:
            raise ValueError("Sandbox scratch directory was replaced; files were preserved")
    else:
        # Older launchers did not save file IDs. Establish ownership through
        # the original explicit writable SID grant before removing that grant.
        if (not any(same_workspace(p,path) for p in report["grants"]) or not api.owns_writable_directory(path,sid)):
            raise ValueError("Scratch ownership cannot be verified; files were preserved")
        report["scratch_identity"] = actual
        save(report)
    return path


def remove_grants(report, api, icacls):
    validate_record(report)
    failures = []
    def failed(operation, target, error):
        failures.append({"operation": operation, "target": str(target), "error": str(error)[:1000]})
    sid = ctypes.c_void_p()
    text = report.get("sid")
    name = report.get("profile")
    if name != "xuloom.sandbox." + report["id"]:
        raise ValueError("Invalid owned sandbox profile")
    if text is not None and (not isinstance(text,str) or not re.fullmatch(r"S-1-15-2-(?:\d+-){6}\d+", text)):
        raise ValueError("Invalid owned AppContainer SID")
    result = api.userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
    if result < 0:
        raise OSError("Cannot verify the owned sandbox SID")
    actual = ctypes.wintypes.LPWSTR()
    verified = False
    try:
        api.check(api.advapi.ConvertSidToStringSidW(sid, ctypes.byref(actual)))
        if text is not None and actual.value != text:
            raise ValueError("Sandbox SID does not match its owned profile")
        text = report["sid"] = actual.value
        verified = True
    finally:
        api.kernel.LocalFree(actual)
        if not verified:
            api.advapi.FreeSid(sid)
    scratch = None
    try:
        try:
            scratch = owned_scratch(report,api,sid)
        except (OSError,ValueError) as error:
            failed("verify_scratch",report.get("scratch"),error)
        if sid.value:
            for path in report.get("traversed", []):
                try:
                    api.traverse(Path(path), sid, remove=True)
                except OSError as error:
                    failed("remove_traverse", path, error)
            for path in reversed(list(dict.fromkeys(report.get("grants", [])))):
                if not os.path.lexists(path):
                    continue
                try:
                    result = subprocess.run([icacls, path, "/L", "/remove", "*" + text], capture_output=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if result.returncode:
                        failed("remove_acl", path, "icacls exit " + str(result.returncode))
                except OSError as error:
                    failed("remove_acl", path, error)
        result = api.userenv.DeleteAppContainerProfile(name)
        if result < 0 and result & 0xffffffff not in {0x80070002, 0x80070490}:
            failed("delete_profile", name, hex(result & 0xffffffff))
    finally:
        if sid.value:
            api.advapi.FreeSid(sid)
    if scratch and os.path.lexists(scratch):
        try:
            if scratch_identity(scratch) != report["scratch_identity"]:
                raise ValueError("Sandbox scratch directory changed during cleanup")
            shutil.rmtree(scratch)
        except (OSError,ValueError) as error:
            failed("remove_scratch", scratch, error)
    report.update(status="cleanup_failed" if failures else "cleaned", failures=failures)
    save(report)
    return report


def retry(workspace, identity):
    if os.name != "nt":
        raise ValueError("Windows sandbox cleanup is unavailable on this platform")
    path = report_path(identity)
    lock = SessionLock(directory(), identity)
    lock.acquire()  # A live helper owns the same lease; never remove its grants.
    try:
        if not path.exists():
            path = directory() / "completed" / path.name
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("id") != identity or not isinstance(report.get("workspace"), str) or not same_workspace(report["workspace"], workspace):
            raise ValueError("Sandbox cleanup record belongs to another workspace")
        if report.get("status") == "cleaned":
            return report
        from xuloom.security.windows_sandbox import WindowsAPI
        return remove_grants(report, WindowsAPI(), str(Path(os.environ["SystemRoot"]) / "System32/icacls.exe"))
    finally:
        lock.close()
