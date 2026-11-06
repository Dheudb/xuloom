"""Cancellable line input shared by local and SSH CLI entry points."""
import asyncio
import os
import stat


class PipeInput:
    """Cancellable UTF-8 line input without a blocked executor thread on exit."""
    def __init__(self, stream):
        self.fd, self.buffer, self.eof = stream.fileno(), b"", False
        self.regular = stat.S_ISREG(os.fstat(self.fd).st_mode)
        if os.name == "nt" and not self.regular:
            import ctypes
            import msvcrt
            from ctypes import wintypes
            self.ctypes, self.count = ctypes, wintypes.DWORD()
            self.handle = msvcrt.get_osfhandle(self.fd)
            self.peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
            self.peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
            self.peek.restype = wintypes.BOOL

    async def read(self):
        while b"\n" not in self.buffer and not self.eof:
            size = 65536
            if not self.regular:
                if os.name == "nt":
                    if not self.peek(self.handle, None, 0, None, self.ctypes.byref(self.count), None):
                        error = self.ctypes.get_last_error()
                        if error in {6, 109, 232}: self.eof = True; break
                        raise self.ctypes.WinError(error)
                    size = min(size, self.count.value)
                else:
                    import select
                    if not select.select([self.fd], [], [], 0)[0]: size = 0
            if not size:
                await asyncio.sleep(.05); continue
            chunk = os.read(self.fd, size)
            if not chunk: self.eof = True
            self.buffer += chunk
            if len(self.buffer) > 1_000_000: raise ValueError("Terminal input exceeds 1 MB")
        if not self.buffer: raise EOFError
        line, separator, rest = self.buffer.partition(b"\n")
        self.buffer = rest if separator else b""
        return line.rstrip(b"\r").decode("utf-8")

