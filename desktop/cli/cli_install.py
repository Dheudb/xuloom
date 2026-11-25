"""Install the portable desktop runtime as a per-user Windows CLI; stdlib only."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import tempfile

OWNER = "xuloom-cli-v1"
HEADER = "@echo off\r\nrem Xuloom user CLI launcher v1\r\n"


def launcher(python):
    # Batch files expand % even inside quotes. Delayed expansion must stay off
    # so installation paths containing ! survive too. Never CALL (double-expand).
    target = str(python).replace("%", "%%")
    return (HEADER + "setlocal DisableDelayedExpansion\r\n"
            + 'for /f "tokens=2 delims=:" %%c in (\'chcp\') do set "_xuloom_old_cp=%%c"\r\n'
            + 'chcp 65001 >nul\r\n'
            + f'if not exist "{target}" (\r\n'
            + '  echo Xuloom runtime not found. Run Install-CLI.cmd from the extracted application. 1>&2\r\n'
            + '  set "_xuloom_exit=1"\r\n  goto xuloom_done\r\n)\r\n'
            + f'"{target}" -I -X utf8 -m xuloom %*\r\n'
            + 'set "_xuloom_exit=%errorlevel%"\r\n:xuloom_done\r\n'
            + 'if defined _xuloom_old_cp chcp %_xuloom_old_cp% >nul\r\n'
            + 'exit /b %_xuloom_exit%\r\n').encode("utf-8")


def path_key(value):
    return os.path.normcase(os.path.abspath(os.path.expandvars(value.strip().strip('"')))).rstrip("\\/")


def add_path(value, directory):
    if any(item.strip() and path_key(item) == path_key(directory) for item in value.split(";")):
        return value, False
    return value + (";" if value else "") + directory, True


def remove_path(value, directory):
    items = value.split(";")
    for index, item in enumerate(items):
        if item.strip() and path_key(item) == path_key(directory):
            del items[index]
            break
    return ";".join(items)


def read_user_path():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, "Path")
        return {"value": value, "kind": kind, "exists": True}
    except FileNotFoundError:
        return {"value": "", "kind": winreg.REG_EXPAND_SZ, "exists": False}


def write_user_path(snapshot):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        if snapshot["exists"]:
            winreg.SetValueEx(key, "Path", 0, snapshot["kind"], snapshot["value"])
        else:
            try:
                winreg.DeleteValue(key, "Path")
            except FileNotFoundError:
                pass


def notify_environment():
    # Existing terminals retain their environment; Explorer can refresh for new ones.
    try:
        result = ctypes.c_size_t()
        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p,
                         ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t)]
        send.restype = ctypes.c_ssize_t
        send(0xFFFF, 0x001A, 0, "Environment", 2, 2000, ctypes.byref(result))
    except (AttributeError, OSError):
        pass


def atomic_write(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(content)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def owned_state(directory):
    command, state = directory / "xuloom.cmd", directory / "cli-install.json"
    if command.is_symlink() or state.is_symlink():
        raise ValueError("Refusing linked CLI files")
    if not command.exists() and not state.exists():
        return None
    try:
        saved = json.loads(state.read_text(encoding="utf-8"))
        if (isinstance(saved, dict) and saved.get("owner") == OWNER
                and type(saved.get("addedPath")) is bool and type(saved.get("pathExisted")) is bool
                and hashlib.sha256(command.read_bytes()).hexdigest() == saved.get("sha256")):
            return saved
    except (OSError, ValueError):
        pass
    raise ValueError("An existing or modified CLI file is present; it will not be overwritten or removed")


def install(package, directory):
    package, directory = Path(package).resolve(), Path(directory).absolute()
    if any(char in str(directory) for char in (';', '"', '\r', '\n')):
        raise ValueError("The command directory cannot contain PATH separators or quotes")
    if directory.is_symlink():
        raise ValueError("Refusing a linked CLI directory")
    python = package / "resources/python/python.exe"
    if not (package / "Xuloom.exe").is_file() or not python.is_file():
        raise ValueError("Extract the complete Windows application before installing the CLI")
    previous = owned_state(directory)
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = read_user_path()
    value, added = add_path(snapshot["value"], str(directory))
    content = launcher(python)
    state = {"owner": OWNER, "python": str(python), "sha256": hashlib.sha256(content).hexdigest(),
             "addedPath": added or bool(previous and previous["addedPath"]),
             "pathExisted": previous["pathExisted"] if previous else snapshot["exists"]}
    files = {directory / "xuloom.cmd": content,
             directory / "cli-install.json": (json.dumps(state, indent=2) + "\n").encode("utf-8")}
    before = {path: path.read_bytes() if path.exists() else None for path in files}
    try:
        for path, data in files.items():
            atomic_write(path, data)
        if added:
            write_user_path({**snapshot, "value": value, "exists": True})
    except Exception:
        for path, data in before.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data)
        raise
    notify_environment()
    return {"installed": True, "command": str(directory / "xuloom.cmd"), "python": str(python), "restartTerminal": True}


def uninstall(directory):
    directory = Path(directory).absolute()
    if directory.is_symlink():
        raise ValueError("Refusing a linked CLI directory")
    state = owned_state(directory)
    if state is None:
        return {"installed": False}
    snapshot = read_user_path()
    if state["addedPath"]:
        value = remove_path(snapshot["value"], str(directory))
        write_user_path({**snapshot, "value": value, "exists": bool(value) or state["pathExisted"]})
    for name in ("xuloom.cmd", "cli-install.json"):
        (directory / name).unlink(missing_ok=True)
    notify_environment()
    return {"installed": False, "configurationPreserved": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--bin-dir", type=Path, help="Override the per-user command directory")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This installer is for the Windows desktop package")
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    directory = args.bin_dir or base / "Xuloom/bin"
    try:
        result = uninstall(directory) if args.uninstall else install(Path(__file__).resolve().parent, directory)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Xuloom CLI: {error}\n")
    print(json.dumps(result))
    print("Close and reopen your terminal to refresh PATH. Then run: xuloom --help")


if __name__ == "__main__":
    main()
