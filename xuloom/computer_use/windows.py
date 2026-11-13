"""One bounded native operation in an owned subprocess on the interactive desktop."""
import base64
import ctypes as c
from ctypes import wintypes as w
from io import BytesIO
import json
import sys


class MouseInput(c.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("data", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra", c.c_size_t)]


class KeyInput(c.Structure):
    _fields_ = [("key", w.WORD), ("scan", w.WORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra", c.c_size_t)]


class InputData(c.Union):
    _fields_ = [("mouse", MouseInput), ("keyboard", KeyInput)]


class Input(c.Structure):
    _fields_ = [("kind", w.DWORD), ("data", InputData)]


class GuiThreadInfo(c.Structure):
    _fields_ = [("size", w.DWORD), ("flags", w.DWORD), ("active", w.HWND), ("focus", w.HWND),
                ("capture", w.HWND), ("menu", w.HWND), ("move_size", w.HWND), ("caret", w.HWND),
                ("caret_rect", w.RECT)]


class BitmapHeader(c.Structure):
    _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG), ("planes", w.WORD),
                ("bits", w.WORD), ("compression", w.DWORD), ("image_size", w.DWORD),
                ("x_resolution", w.LONG), ("y_resolution", w.LONG), ("colors", w.DWORD),
                ("important_colors", w.DWORD)]


if sys.platform == "win32":
    user32 = c.WinDLL("user32", use_last_error=True)
    gdi32 = c.WinDLL("gdi32", use_last_error=True)
    kernel32 = c.WinDLL("kernel32", use_last_error=True)
    dwmapi = c.WinDLL("dwmapi", use_last_error=True)
    WindowCallback = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def bind(dll, name, result, *args):
        function = getattr(dll, name)
        function.restype, function.argtypes = result, args
        return function

    dpi = bind(user32, "SetThreadDpiAwarenessContext", w.HANDLE, w.HANDLE)
    input_desktop = bind(user32, "OpenInputDesktop", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
    thread_desktop = bind(user32, "GetThreadDesktop", w.HANDLE, w.DWORD)
    desktop_info = bind(user32, "GetUserObjectInformationW", w.BOOL, w.HANDLE, c.c_int,
                        w.LPVOID, w.DWORD, c.POINTER(w.DWORD))
    close_desktop = bind(user32, "CloseDesktop", w.BOOL, w.HANDLE)
    enum_windows = bind(user32, "EnumWindows", w.BOOL, WindowCallback, w.LPARAM)
    window_visible = bind(user32, "IsWindowVisible", w.BOOL, w.HWND)
    minimized = bind(user32, "IsIconic", w.BOOL, w.HWND)
    window_title = bind(user32, "GetWindowTextW", c.c_int, w.HWND, w.LPWSTR, c.c_int)
    window_process = bind(user32, "GetWindowThreadProcessId", w.DWORD, w.HWND, c.POINTER(w.DWORD))
    window_bounds = bind(user32, "GetClientRect", w.BOOL, w.HWND, c.POINTER(w.RECT))
    to_screen = bind(user32, "ClientToScreen", w.BOOL, w.HWND, c.POINTER(w.POINT))
    window_attribute = bind(dwmapi, "DwmGetWindowAttribute", w.LONG, w.HWND, w.DWORD, w.LPVOID, w.DWORD)
    foreground = bind(user32, "GetForegroundWindow", w.HWND)
    display_affinity = bind(user32, "GetWindowDisplayAffinity", w.BOOL, w.HWND, c.POINTER(w.DWORD))
    point_window = bind(user32, "WindowFromPoint", w.HWND, w.POINT)
    ancestor = bind(user32, "GetAncestor", w.HWND, w.HWND, w.UINT)
    move_pointer = bind(user32, "SetCursorPos", w.BOOL, c.c_int, c.c_int)
    cursor_position = bind(user32, "GetCursorPos", w.BOOL, c.POINTER(w.POINT))
    key_state = bind(user32, "GetAsyncKeyState", w.SHORT, c.c_int)
    gui_info = bind(user32, "GetGUIThreadInfo", w.BOOL, w.DWORD, c.POINTER(GuiThreadInfo))
    send_input = bind(user32, "SendInput", w.UINT, w.UINT, c.POINTER(Input), c.c_int)
    window_dc = bind(user32, "GetDC", w.HDC, w.HWND)
    release_dc = bind(user32, "ReleaseDC", c.c_int, w.HWND, w.HDC)
    print_window = bind(user32, "PrintWindow", w.BOOL, w.HWND, w.HDC, w.UINT)
    memory_dc = bind(gdi32, "CreateCompatibleDC", w.HDC, w.HDC)
    delete_dc = bind(gdi32, "DeleteDC", w.BOOL, w.HDC)
    bitmap_create = bind(gdi32, "CreateDIBSection", w.HBITMAP, w.HDC, c.POINTER(BitmapHeader),
                         w.UINT, c.POINTER(w.LPVOID), w.HANDLE, w.DWORD)
    select_object = bind(gdi32, "SelectObject", w.HANDLE, w.HDC, w.HANDLE)
    delete_object = bind(gdi32, "DeleteObject", w.BOOL, w.HANDLE)
    open_process = bind(kernel32, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
    process_times = bind(kernel32, "GetProcessTimes", w.BOOL, w.HANDLE, *([c.POINTER(w.FILETIME)] * 4))
    close_handle = bind(kernel32, "CloseHandle", w.BOOL, w.HANDLE)
    thread_id = bind(kernel32, "GetCurrentThreadId", w.DWORD)
    create_mutex = bind(kernel32, "CreateMutexW", w.HANDLE, w.LPVOID, w.BOOL, w.LPCWSTR)
    wait_mutex = bind(kernel32, "WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)
    release_mutex = bind(kernel32, "ReleaseMutex", w.BOOL, w.HANDLE)


def window_identity(hwnd):
    if not hwnd or not window_visible(hwnd) or ancestor(hwnd, 2) != hwnd:
        return None
    cloaked = w.DWORD()
    if window_attribute(hwnd, 14, c.byref(cloaked), c.sizeof(cloaked)) == 0 and cloaked.value:
        return None
    title = c.create_unicode_buffer(161)
    if not window_title(hwnd, title, len(title)):
        return None
    pid = w.DWORD()
    thread = window_process(hwnd, c.byref(pid))
    process = open_process(0x1000, False, pid.value)
    if not process:
        return None
    try:
        times = [w.FILETIME() for _ in range(4)]
        if not process_times(process, *(c.byref(value) for value in times)):
            return None
    finally:
        close_handle(process)
    started = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    title_text = "".join(char if char.isprintable() else " " for char in title.value)
    return {"hwnd": hwnd, "pid": pid.value, "thread": thread, "started": started, "title": title_text}


def require_active_desktop():
    def name(handle):
        buffer = c.create_unicode_buffer(256)
        size = w.DWORD()
        if handle and desktop_info(handle, 2, buffer, c.sizeof(buffer), c.byref(size)):
            return buffer.value
        return ""

    active = input_desktop(0, False, 1)
    if active:
        try:
            own = name(thread_desktop(thread_id()))
            if own and own == name(active):
                return
        finally:
            close_desktop(active)
    raise ValueError("Unlock the active Windows desktop before using Computer Use.")


def list_windows():
    windows = []

    @WindowCallback
    def collect(hwnd, _):
        if len(windows) >= 40:
            return False
        identity = window_identity(hwnd)
        if identity:
            windows.append(identity)
        return True

    enum_windows(collect, 0)
    return {"windows": windows}


def target_bounds(target):
    hwnd = target["hwnd"]
    current = window_identity(hwnd)
    if current is None or any(current[key] != target[key] for key in ("pid", "thread", "started")):
        raise ValueError("The selected window closed or changed. List windows again.")
    area, origin = w.RECT(), w.POINT()
    if minimized(hwnd) or not window_bounds(hwnd, c.byref(area)) or not to_screen(hwnd, c.byref(origin)):
        raise ValueError("Restore the selected window before continuing.")
    width, height = area.right, area.bottom
    if width < 1 or height < 1 or width * height > 16_000_000:
        raise ValueError("The window has no usable client area. Restore or resize it.")
    return [origin.x, origin.y, width, height]


def screenshot(target):
    from PIL import Image

    bounds = target_bounds(target)
    _, _, width, height = bounds
    hwnd = target["hwnd"]
    focus = window_focus(target)
    affinity = w.DWORD()
    if display_affinity(hwnd, c.byref(affinity)) and affinity.value:
        raise ValueError("This window is protected from capture.")
    dc = window_dc(hwnd)
    if not dc:
        raise OSError("Windows could not open the selected window for capture.")
    memory = bitmap = old = None
    try:
        memory = memory_dc(dc)
        pixels = w.LPVOID()
        header = BitmapHeader(c.sizeof(BitmapHeader), width, -height, 1, 32)
        if memory:
            bitmap = bitmap_create(memory, c.byref(header), 0, c.byref(pixels), None, 0)
        if bitmap:
            old = select_object(memory, bitmap)
        if not old or not print_window(hwnd, memory, 3):
            raise OSError("Windows could not capture this window.")
        image = Image.frombytes("RGB", (width, height), c.string_at(pixels, width * height * 4), "raw", "BGRX")
    finally:
        if old:
            select_object(memory, old)
        if bitmap:
            delete_object(bitmap)
        if memory:
            delete_dc(memory)
        release_dc(hwnd, dc)
    if bounds != target_bounds(target):
        raise ValueError("The window moved or resized during capture. Observe it again.")
    if image.getbbox() is None:
        raise OSError("The window returned a black capture. Try a different window.")
    image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    png = BytesIO()
    image.save(png, format="PNG")
    if focus != window_focus(target):
        raise ValueError("Keyboard focus changed during capture. Observe the window again.")
    return {"bounds": bounds, "size": list(image.size), "focus": focus,
            "png": base64.b64encode(png.getvalue()).decode("ascii")}


def window_focus(target):
    info = GuiThreadInfo(size=c.sizeof(GuiThreadInfo))
    return info.focus if gui_info(target["thread"], c.byref(info)) else None


def verify_input_target(target, bounds):
    hwnd = target["hwnd"]
    if foreground() != hwnd:
        raise ValueError("Bring the selected window to the foreground, then observe it again.")
    if bounds != target_bounds(target):
        raise ValueError("The selected window moved or resized. Observe it again.")
    info = GuiThreadInfo(size=c.sizeof(GuiThreadInfo))
    if not gui_info(target["thread"], c.byref(info)) or not info.focus or ancestor(info.focus, 2) != hwnd:
        raise ValueError("The selected window does not own keyboard focus. Observe it again.")
    if info.flags & 0x1E or info.capture and ancestor(info.capture, 2) != hwnd:
        raise ValueError("Close the active menu or finish moving the window, then observe again.")
    if any(key_state(key) & 0x8000 for key in (1, 2, 4, 0x10, 0x11, 0x12, 0x5B, 0x5C)):
        raise ValueError("Release held mouse buttons or modifier keys, then observe again.")
    return info.focus


def keyboard_events(value, *, unicode=False):
    base = 4 if unicode else (1 if value in {35, 36, 37, 38, 39, 40, 46} else 0)
    for flag in (0, 2):
        yield Input(1, InputData(keyboard=KeyInput(
            0 if unicode else value, value if unicode else 0, flag | base)))


KEYS = {"enter": 13, "tab": 9, "escape": 27, "backspace": 8, "delete": 46,
        "left": 37, "up": 38, "right": 39, "down": 40, "home": 36, "end": 35}


def deliver(events):
    sent = send_input(len(events), (Input * len(events))(*events), c.sizeof(Input))
    if sent != len(events):
        # Events are down/up pairs. Release the last key/button if only its down event was accepted.
        if sent % 2:
            send_input(1, c.byref(events[sent]), c.sizeof(Input))
        raise OSError("Windows blocked or only partially delivered input. Observe the result before continuing.")


def act(request):
    target, bounds, size = request["target"], request["bounds"], request["size"]
    hwnd, action = target["hwnd"], request["action"]
    focus = verify_input_target(target, bounds)
    if action in {"move", "click", "scroll"}:
        x, y = request["x"], request["y"]
        if not (0 <= x < size[0] and 0 <= y < size[1]):
            raise ValueError("Coordinates are outside the latest screenshot.")
        point = w.POINT(bounds[0] + (2*x + 1)*bounds[2] // (2*size[0]),
                        bounds[1] + (2*y + 1)*bounds[3] // (2*size[1]))
        if ancestor(point_window(point), 2) != hwnd or not move_pointer(point.x, point.y):
            raise ValueError("The point is covered or outside the selected window.")
        if action != "move":
            actual = w.POINT()
            verify_input_target(target, bounds)
            if (not cursor_position(c.byref(actual)) or (actual.x, actual.y) != (point.x, point.y)
                    or ancestor(point_window(actual), 2) != hwnd):
                raise ValueError("The pointer or window changed. Observe again before sending input.")
            flags = (2, 4) if action == "click" else (0x800,)
            deliver([Input(0, InputData(mouse=MouseInput(data=120 * (request.get("steps") or 0), flags=flag)))
                     for flag in flags])
    else:
        if focus != request["focus"]:
            raise ValueError("Keyboard focus changed since the screenshot. Observe again before sending input.")
        if action == "key":
            deliver(list(keyboard_events(KEYS[request["key"]])))
            return {}
        text = request["text"]
        for start in range(0, len(text), 32):
            if verify_input_target(target, bounds) != focus:
                raise ValueError("Keyboard focus changed during input. Some text may already have been entered.")
            units = text[start:start+32].encode("utf-16-le")
            deliver([event for i in range(0, len(units), 2)
                     for event in keyboard_events(int.from_bytes(units[i:i+2], "little"), unicode=True)])
    return {}


def run(request):
    require_active_desktop()
    if not dpi(w.HANDLE(-4)):  # Per-monitor V2: all geometry is in physical pixels.
        raise OSError("Windows could not establish physical-pixel coordinates.")
    if request["action"] == "list":
        return list_windows()
    mutex = create_mutex(None, False, "Local\\XuloomComputerUse")
    if not mutex:
        raise OSError("Windows could not reserve desktop input.")
    try:
        if wait_mutex(mutex, 0) not in {0, 0x80}:
            raise ValueError("Another Xuloom window operation is active. Try again after it finishes.")
        try:
            return screenshot(request["target"]) if request["action"] == "observe" else act(request)
        finally:
            release_mutex(mutex)
    finally:
        close_handle(mutex)


if __name__ == "__main__":
    try:
        if sys.platform != "win32":
            raise ValueError("Computer Use requires Windows.")
        result = run(json.loads(sys.stdin.buffer.readline(65536)))
    except (OSError, ValueError, KeyError) as error:
        result = {"error": str(error)}
    print(json.dumps(result, ensure_ascii=True), flush=True)
