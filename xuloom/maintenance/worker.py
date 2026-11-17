"""Deferred restore after a normal application exit; never terminates a process."""
import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent', type=int, required=True)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--status', required=True)
    args = parser.parse_args()
    from xuloom.core.storage import atomic_json
    try:
        if os.name != 'nt':
            raise ValueError('Desktop deferred restore currently requires Windows')
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, args.parent)
        if not handle:
            raise OSError('Cannot wait for application exit')
        atomic_json(Path(args.status), {'state': 'waiting'})
        try:
            if kernel.WaitForSingleObject(handle, 12 * 60 * 60 * 1000) != 0:
                raise TimeoutError('Application did not exit; data was not changed')
        finally:
            kernel.CloseHandle(handle)
        from xuloom.maintenance.archive import restore
        result = restore(args.data_root, args.archive, confirmed=True)
        atomic_json(Path(args.status), {'state': 'complete', **result})
    except Exception as error:
        atomic_json(Path(args.status), {'state': 'failed', 'error': str(error)})


if __name__ == '__main__':
    main()
