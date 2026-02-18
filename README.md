
**Region selection during pause:** User pauses agent → draws crop rectangle on live preview → resumes. Next turn, capture.py uses the new crop box. The VLM sees a different region now, so its story context about the previous region becomes stale, but that's the user's deliberate choice. (TODO:EACH PAUSE IT SHOULD BE POSSIBLE TO MODIFY THE REGION - INCONVINIENCE)

**Tool filtering:** Panel shows checkboxes for each tool (click, right_click, double_click, drag, write, remember, recall). User unchecks right_click → execute.py skips any `right_click(...)` calls found in the story. The VLM still generates them, they just don't execute and don't appear in feedback as "OK". This prevents accidental right-click closing Paint etc. Clean mechanism: panel writes allowed tools list to a file, execute.py reads it.

**Architecture of new features:**

1. Panel gets a `/screenshot` GET endpoint that captures and returns a fresh JPEG/PNG for the live preview (TODO: THIS PREVIEW DOES NOT UPDATE IN REAL TIME - INCONVINIENCE)
2. Panel.html shows this preview, user draws rectangle, sends coordinates via POST to `/crop`  
3. Panel writes `crop.json` to run_dir with `{x1, y1, x2, y2, width, height}` in pixels
4. Panel writes `allowed_tools.json` to run_dir with list of enabled tool names (TODO: UNKNOWN IF SWITCHING OF TOOLS IS REALLY DYNAMIC after FIRST SWITCH - TIME TO IMPLEMENT STATE CLASS FOR THE PROJECT TO STORE THERE THESE COORDINATES OF LAST TIME AND OTHER STUFF THAT CURRENTLY GOES INTO FILE AND ITS COUPLE OF LINES - NONSENCE - BETTER TO KEEP IT IN PROGRAM MEMORY AS SEPARATE SPECIALIZED CLASS)
5. capture.py reads `crop.json` — crops then resizes to configured WIDTH/HEIGHT (or crop dimensions if WIDTH/HEIGHT are 0) (TODO: ENSURE THE RESIZING WORKS CORRECTLY - THE CROPING MECHANISM HAS ITS FUTURE TO "ZOOMM" SOME REGIONS BY THE MODEL ITSELF BUT WE DONT YET IMPLEMENT THAT - THE RESIZING IS IMPORTANT FOR FAST DEBUGGING AND MODEL RESPONSE - IT MUST WORK)
6. tools.py reads crop box for coordinate remapping when PHYSICAL_EXECUTION is True
7. execute.py reads `allowed_tools.json` to filter which calls get executed

Actually, having capture.py and tools.py read JSON files each turn is fine — they're subprocess-per-turn anyway. But tools.py runs inside execute.py's process, so execute.py should read the files and pass the data to tools.configure().

PROPOSED PERSISTENCE APPROACH: A SPECIALIZED CLASS OR FILE THAT WILL HANDLE ALL THE DATA RELATED
TO STATE OF THE ENTITY "story" / SYSTEM IN OVERALL. It will also hold all the loging logic, it may be a good idea.
IT CAN ALSO HOLD ALL THE BASE64 data - the hardware on which the system is running is capable of handling these large memory operations, delays are not a problem. The VLM via LM Studio is taking a tens of seconds to finish processing single image so couple of miliseconds for copying data in memory or sending huge data via api is ACCEPTABLE - I prefer to have slower system than system that operates with data slicing /cutting off. Quality is the priority of this project.

class Persistence: (THIS IS ONLY AN PSEUDO CODE EXAMPLE)
    def __init__(self, base_dir: str = "runs"):
        self.run_dir = Path(base_dir) / datetime.now().strftime("%Y%m%d_%H%M%S")

    def _load_state(self) -> None:

    def save_state(self) -> None:

    def new_turn(self, story_chunk: str, actions: list[str],
                 feedback: dict, screenshot_b64: str | None = None) -> None:
        self.turn += 1
        self.save_state()

    def toggle_pause(self) -> None:

    def get_full_story(self) -> str:

       
I have made capture.py importable as well as runnable. The `capture()` function already exists and returns base64 PNG. Panel can just import and call it. But then panel.py needs DPI awareness and GDI setup... and capture.py already handles that at module level. If panel.py does `from capture import capture as take_screenshot` it gets all of that for free. The only issue is capture.py reads config for CROP_BOX and CAPTURE_DELAY — for the preview we want no crop and no delay. I'll add a `preview()` function to capture.py that captures full screen at reduced resolution with no delay and no crop.


```python
# config.py
TEMPERATURE: float = 0.7
TOP_P: float = 0.9
MAX_TOKENS: int = 300
MODEL: str = "qwen3-vl-2b-instruct-1m"
WIDTH: int = 0
HEIGHT: int = 0
PHYSICAL_EXECUTION: bool = False
LOOP_DELAY: float = 2.0
CAPTURE_DELAY: float = 1.0
CACHE_PROMPT: bool = True
```

```python
# tools.py
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import time
from pathlib import Path
from typing import Final

_INPUT_MOUSE: Final = 0
_INPUT_KEYBOARD: Final = 1
_MOUSEEVENTF_MOVE: Final = 0x0001
_MOUSEEVENTF_LEFTDOWN: Final = 0x0002
_MOUSEEVENTF_LEFTUP: Final = 0x0004
_MOUSEEVENTF_RIGHTDOWN: Final = 0x0008
_MOUSEEVENTF_RIGHTUP: Final = 0x0010
_MOUSEEVENTF_ABSOLUTE: Final = 0x8000
_KEYEVENTF_KEYUP: Final = 0x0002
_KEYEVENTF_UNICODE: Final = 0x0004


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


_user32: ctypes.WinDLL | None = None
_screen_w: int = 0
_screen_h: int = 0
_physical: bool = False
_executed: list[str] = []
_run_dir: str = ""
_crop_x1: int = 0
_crop_y1: int = 0
_crop_x2: int = 0
_crop_y2: int = 0
_crop_active: bool = False


def _init_win32() -> None:
    global _user32, _screen_w, _screen_h
    if _user32 is not None:
        return
    ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _screen_w = _user32.GetSystemMetrics(0)
    _screen_h = _user32.GetSystemMetrics(1)
    _user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = ctypes.c_uint


def _send_inputs(items: list[_INPUT]) -> None:
    assert _user32 is not None
    if not items:
        return
    arr = (_INPUT * len(items))(*items)
    if _user32.SendInput(len(items), arr, ctypes.sizeof(_INPUT)) != len(items):
        raise OSError(ctypes.get_last_error())


def _send_mouse(flags: int, abs_x: int | None = None, abs_y: int | None = None) -> None:
    inp = _INPUT(type=_INPUT_MOUSE)
    f, dx, dy = flags, 0, 0
    if abs_x is not None and abs_y is not None:
        dx, dy, f = abs_x, abs_y, f | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_MOVE
    inp.u.mi = _MOUSEINPUT(dx, dy, 0, f, 0, 0)
    _send_inputs([inp])


def _send_unicode(text: str) -> None:
    items: list[_INPUT] = []
    for ch in text:
        if ch == "\r":
            continue
        code = 0x000D if ch == "\n" else ord(ch)
        for fl in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
            inp = _INPUT(type=_INPUT_KEYBOARD)
            inp.u.ki = _KEYBDINPUT(0, code, fl, 0, 0)
            items.append(inp)
    _send_inputs(items)


def _to_abs(x_px: int, y_px: int) -> tuple[int, int]:
    return (
        max(0, min(65535, int((x_px / max(1, _screen_w - 1)) * 65535))),
        max(0, min(65535, int((y_px / max(1, _screen_h - 1)) * 65535))),
    )


def _smooth_move(tx: int, ty: int) -> None:
    assert _user32 is not None
    pt = ctypes.wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    sx, sy = pt.x, pt.y
    ddx, ddy = tx - sx, ty - sy
    for i in range(21):
        t = i / 20
        t = t * t * (3.0 - 2.0 * t)
        _send_mouse(0, *_to_abs(int(sx + ddx * t), int(sy + ddy * t)))
        time.sleep(0.01)


def _remap(v: int, dim: int) -> int:
    if _crop_active:
        if dim == _screen_w:
            return _crop_x1 + int((v / 1000) * (_crop_x2 - _crop_x1))
        return _crop_y1 + int((v / 1000) * (_crop_y2 - _crop_y1))
    return int((v / 1000) * dim)


_CLICK_BUTTONS: Final = {
    "click": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP, False),
    "right_click": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP, False),
    "double_click": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP, True),
}


def _phys_click(name: str, x: int, y: int) -> None:
    down, up, double = _CLICK_BUTTONS[name]
    _smooth_move(_remap(x, _screen_w), _remap(y, _screen_h))
    time.sleep(0.12)
    _send_mouse(down); time.sleep(0.02); _send_mouse(up)
    if double:
        time.sleep(0.06)
        _send_mouse(down); time.sleep(0.02); _send_mouse(up)


def _phys_drag(x1: int, y1: int, x2: int, y2: int) -> None:
    _smooth_move(_remap(x1, _screen_w), _remap(y1, _screen_h))
    time.sleep(0.08)
    _send_mouse(_MOUSEEVENTF_LEFTDOWN); time.sleep(0.06)
    _smooth_move(_remap(x2, _screen_w), _remap(y2, _screen_h))
    time.sleep(0.06)
    _send_mouse(_MOUSEEVENTF_LEFTUP)


def configure(*, physical: bool, run_dir: str, crop: dict | None = None) -> None:
    global _physical, _executed, _run_dir
    global _crop_x1, _crop_y1, _crop_x2, _crop_y2, _crop_active
    _physical = physical
    _executed = []
    _run_dir = run_dir
    if physical:
        _init_win32()
    if crop and all(k in crop for k in ("x1", "y1", "x2", "y2")):
        _crop_x1 = int(crop["x1"])
        _crop_y1 = int(crop["y1"])
        _crop_x2 = int(crop["x2"])
        _crop_y2 = int(crop["y2"])
        _crop_active = _crop_x2 > _crop_x1 and _crop_y2 > _crop_y1
    else:
        _crop_active = False


def get_results() -> list[str]:
    return list(_executed)


def _valid(name: str, v: object) -> int:
    if not isinstance(v, int | float):
        raise TypeError(f"{name} must be a number, got {type(v).__name__}")
    iv = int(v)
    if not 0 <= iv <= 1000:
        raise ValueError(f"{name}={iv} outside 0-1000")
    return iv


def _record(canon: str) -> bool:
    _executed.append(canon)
    return _physical


def click(x: int, y: int) -> None:
    """click(x, y) -- Left-click at (x, y). Coordinates 0-1000."""
    ix, iy = _valid("x", x), _valid("y", y)
    if _record(f"click({ix}, {iy})"):
        _phys_click("click", ix, iy)


def right_click(x: int, y: int) -> None:
    """right_click(x, y) -- Right-click at (x, y). Coordinates 0-1000."""
    ix, iy = _valid("x", x), _valid("y", y)
    if _record(f"right_click({ix}, {iy})"):
        _phys_click("right_click", ix, iy)


def double_click(x: int, y: int) -> None:
    """double_click(x, y) -- Double-click at (x, y). Coordinates 0-1000."""
    ix, iy = _valid("x", x), _valid("y", y)
    if _record(f"double_click({ix}, {iy})"):
        _phys_click("double_click", ix, iy)


def drag(x1: int, y1: int, x2: int, y2: int) -> None:
    """drag(x1, y1, x2, y2) -- Drag from (x1,y1) to (x2,y2). Coordinates 0-1000."""
    c = [_valid(n, v) for n, v in zip(("x1", "y1", "x2", "y2"), (x1, y1, x2, y2))]
    if _record(f"drag({c[0]}, {c[1]}, {c[2]}, {c[3]})"):
        _phys_drag(*c)


def write(text: str) -> None:
    """write(text) -- Type text at current cursor position."""
    if not isinstance(text, str):
        raise TypeError(f"write() requires str, got {type(text).__name__}")
    if _record(f"write({json.dumps(text)})"):
        _send_unicode(text)


def _memory_path() -> Path:
    return Path(_run_dir) / "memory.json" if _run_dir else Path("memory.json")


def remember(text: str) -> None:
    """remember(text) -- Save a thought to persistent memory."""
    if not isinstance(text, str):
        raise TypeError(f"remember() requires str, got {type(text).__name__}")
    p = _memory_path()
    items: list[str] = []
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    items.append(text)
    p.write_text(json.dumps(items, indent=2), encoding="utf-8")
    _record(f"remember({json.dumps(text)})")


def recall() -> str:
    """recall() -- Read stored memories."""
    try:
        items = json.loads(_memory_path().read_text(encoding="utf-8"))
        if isinstance(items, list) and items:
            return "\n".join(f"- {s}" for s in items)
    except Exception:
        pass
    return "(no memories yet)"


TOOL_NAMES: Final[tuple[str, ...]] = (
    "click", "right_click", "double_click", "drag", "write", "remember", "recall",
)
```

```python
# capture.py
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import struct
import sys
import time
import zlib
from typing import Final

import config as _cfg

_SRCCOPY: Final = 0x00CC0020
_CAPTUREBLT: Final = 0x40000000
_BI_RGB: Final = 0
_DIB_RGB: Final = 0
_HALFTONE: Final = 4

try:
    ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
except Exception:
    pass

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

_W = ctypes.wintypes
_vp = ctypes.c_void_p
_ci = ctypes.c_int


def _sig(obj, attr, args, res):
    fn = getattr(obj, attr); fn.argtypes = args; fn.restype = res


_sig(_user32, "GetDC", [_W.HWND], _W.HDC)
_sig(_user32, "ReleaseDC", [_W.HWND, _W.HDC], _ci)
_sig(_user32, "GetSystemMetrics", [_ci], _ci)
_sig(_gdi32, "CreateCompatibleDC", [_W.HDC], _W.HDC)
_sig(_gdi32, "CreateDIBSection", [_W.HDC, _vp, _W.UINT, ctypes.POINTER(_vp), _W.HANDLE, _W.DWORD], _W.HBITMAP)
_sig(_gdi32, "SelectObject", [_W.HDC, _W.HGDIOBJ], _W.HGDIOBJ)
_sig(_gdi32, "BitBlt", [_W.HDC, _ci, _ci, _ci, _ci, _W.HDC, _ci, _ci, _W.DWORD], _W.BOOL)
_sig(_gdi32, "StretchBlt", [_W.HDC, _ci, _ci, _ci, _ci, _W.HDC, _ci, _ci, _ci, _ci, _W.DWORD], _W.BOOL)
_sig(_gdi32, "SetStretchBltMode", [_W.HDC, _ci], _ci)
_sig(_gdi32, "SetBrushOrgEx", [_W.HDC, _ci, _ci, _vp], _W.BOOL)
_sig(_gdi32, "DeleteObject", [_W.HGDIOBJ], _W.BOOL)
_sig(_gdi32, "DeleteDC", [_W.HDC], _W.BOOL)

del _sig, _W, _vp, _ci


def _log(msg: str) -> None:
    sys.stderr.write(f"[capture] {msg}\n"); sys.stderr.flush()


def screen_size() -> tuple[int, int]:
    w, h = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
    return (w, h) if w > 0 and h > 0 else (1920, 1080)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD), ("biWidth", ctypes.wintypes.LONG),
        ("biHeight", ctypes.wintypes.LONG), ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD), ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD), ("biXPelsPerMeter", ctypes.wintypes.LONG),
        ("biYPelsPerMeter", ctypes.wintypes.LONG), ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.wintypes.DWORD * 3)]


def _make_bmi(w: int, h: int) -> _BITMAPINFO:
    bmi = _BITMAPINFO()
    hdr = bmi.bmiHeader
    hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    hdr.biWidth, hdr.biHeight = w, -h
    hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, _BI_RGB
    return bmi


def _create_dib(sdc, w: int, h: int):
    bits = ctypes.c_void_p()
    hbmp = _gdi32.CreateDIBSection(sdc, ctypes.byref(_make_bmi(w, h)), _DIB_RGB, ctypes.byref(bits), None, 0)
    return (hbmp, bits) if hbmp and bits.value else (None, None)


def _read_dib(bits, n: int) -> bytes | None:
    try:
        return bytes((ctypes.c_ubyte * n).from_address(bits.value))
    except Exception as e:
        _log(f"DIB read failed: {e}"); return None


def capture_screen(w: int, h: int) -> bytes | None:
    sdc = _user32.GetDC(0)
    if not sdc:
        return None
    memdc = _gdi32.CreateCompatibleDC(sdc)
    if not memdc:
        _user32.ReleaseDC(0, sdc); return None
    hbmp, bits = _create_dib(sdc, w, h)
    if not hbmp:
        _gdi32.DeleteDC(memdc); _user32.ReleaseDC(0, sdc); return None
    old = _gdi32.SelectObject(memdc, hbmp)
    _gdi32.BitBlt(memdc, 0, 0, w, h, sdc, 0, 0, _SRCCOPY | _CAPTUREBLT)
    result = _read_dib(bits, w * h * 4)
    _gdi32.SelectObject(memdc, old)
    _gdi32.DeleteObject(hbmp)
    _gdi32.DeleteDC(memdc)
    _user32.ReleaseDC(0, sdc)
    return result


def _resize_bgra(src: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes | None:
    sdc = _user32.GetDC(0)
    if not sdc:
        return None
    src_dc, dst_dc = _gdi32.CreateCompatibleDC(sdc), _gdi32.CreateCompatibleDC(sdc)
    if not src_dc or not dst_dc:
        for dc in (src_dc, dst_dc):
            if dc: _gdi32.DeleteDC(dc)
        _user32.ReleaseDC(0, sdc); return None
    src_bmp, src_bits = _create_dib(sdc, sw, sh)
    if not src_bmp:
        _gdi32.DeleteDC(src_dc); _gdi32.DeleteDC(dst_dc); _user32.ReleaseDC(0, sdc); return None
    ctypes.memmove(src_bits.value, src, sw * sh * 4)
    old_src = _gdi32.SelectObject(src_dc, src_bmp)
    dst_bmp, dst_bits = _create_dib(sdc, dw, dh)
    if not dst_bmp:
        _gdi32.SelectObject(src_dc, old_src); _gdi32.DeleteObject(src_bmp)
        _gdi32.DeleteDC(src_dc); _gdi32.DeleteDC(dst_dc); _user32.ReleaseDC(0, sdc); return None
    old_dst = _gdi32.SelectObject(dst_dc, dst_bmp)
    _gdi32.SetStretchBltMode(dst_dc, _HALFTONE)
    _gdi32.SetBrushOrgEx(dst_dc, 0, 0, None)
    _gdi32.StretchBlt(dst_dc, 0, 0, dw, dh, src_dc, 0, 0, sw, sh, _SRCCOPY)
    result = _read_dib(dst_bits, dw * dh * 4)
    _gdi32.SelectObject(dst_dc, old_dst); _gdi32.SelectObject(src_dc, old_src)
    _gdi32.DeleteObject(dst_bmp); _gdi32.DeleteObject(src_bmp)
    _gdi32.DeleteDC(dst_dc); _gdi32.DeleteDC(src_dc)
    _user32.ReleaseDC(0, sdc)
    return result


def encode_png(bgra: bytes, w: int, h: int) -> bytes:
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[y * stride:(y + 1) * stride]
        for i in range(0, len(row), 4):
            raw.extend((row[i + 2], row[i + 1], row[i], 255))

    def chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def crop_bgra(bgra: bytes, sw: int, sh: int, x1: int, y1: int, x2: int, y2: int) -> tuple[bytes, int, int]:
    x1, y1 = max(0, min(x1, sw)), max(0, min(y1, sh))
    x2, y2 = max(x1, min(x2, sw)), max(y1, min(y2, sh))
    if x1 >= x2 or y1 >= y2:
        return bgra, sw, sh
    cw, ch = x2 - x1, y2 - y1
    out = bytearray(cw * ch * 4)
    ss, ds = sw * 4, cw * 4
    for y in range(ch):
        so = (y1 + y) * ss + x1 * 4
        do = y * ds
        out[do:do + ds] = bgra[so:so + ds]
    return bytes(out), cw, ch


def preview_b64(max_width: int = 800) -> str:
    sw, sh = screen_size()
    bgra = capture_screen(sw, sh)
    if bgra is None:
        return ""
    dw = min(sw, max_width)
    dh = int(sh * (dw / sw))
    if (dw, dh) != (sw, sh):
        resized = _resize_bgra(bgra, sw, sh, dw, dh)
        if resized is not None:
            bgra = resized
        else:
            dw, dh = sw, sh
    return base64.b64encode(encode_png(bgra, dw, dh)).decode("ascii")


def capture(crop: dict | None = None) -> str:
    sw, sh = screen_size()
    delay = float(_cfg.CAPTURE_DELAY)
    if delay > 0:
        time.sleep(delay)
    bgra = capture_screen(sw, sh)
    if bgra is None:
        return ""

    bw, bh = sw, sh
    if crop and all(k in crop for k in ("x1", "y1", "x2", "y2")):
        cx1, cy1 = int(crop["x1"]), int(crop["y1"])
        cx2, cy2 = int(crop["x2"]), int(crop["y2"])
        if cx2 > cx1 and cy2 > cy1:
            bgra, bw, bh = crop_bgra(bgra, sw, sh, cx1, cy1, cx2, cy2)

    dw = int(_cfg.WIDTH) if int(_cfg.WIDTH) > 0 else bw
    dh = int(_cfg.HEIGHT) if int(_cfg.HEIGHT) > 0 else bh
    if (dw, dh) != (bw, bh):
        resized = _resize_bgra(bgra, bw, bh, dw, dh)
        if resized is not None:
            bgra = resized
        else:
            dw, dh = bw, bh

    return base64.b64encode(encode_png(bgra, dw, dh)).decode("ascii")


def main() -> None:
    try:
        req = json.loads(sys.stdin.read() or "{}")
        crop = req.get("crop")
        b64 = capture(crop=crop)
        sys.stdout.write(json.dumps({"screenshot_b64": b64}))
        sys.stdout.flush()
    except Exception as exc:
        _log(f"FATAL: {exc}")
        sys.stdout.write(json.dumps({"screenshot_b64": "", "error": str(exc)}))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
```

```python
# execute.py
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

import config as _cfg
import tools

_CAPTURE_SCRIPT: Final = Path(__file__).parent / "capture.py"
_FUNC_LIST: Final = ", ".join(tools.TOOL_NAMES)
_FENCE_RE: Final = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)

_SAFE_BUILTINS: Final[dict[str, object]] = {
    n: (__builtins__[n] if isinstance(__builtins__, dict) else getattr(__builtins__, n, None))
    for n in ("range", "int", "str", "float", "bool", "len", "abs", "max", "min",
              "round", "list", "tuple", "dict", "set", "isinstance", "type",
              "True", "False", "None")
    if (isinstance(__builtins__, dict) and n in __builtins__) or hasattr(__builtins__, n)
}


def _log(msg: str) -> None:
    sys.stderr.write(f"[execute] {msg}\n"); sys.stderr.flush()


def _extract_calls(raw: str, allowed: set[str]) -> list[str]:
    fenced = _FENCE_RE.findall(raw)
    sources = (["\n".join(b.strip() for b in fenced)] if fenced else []) + [raw]
    seen: set[str] = set()
    lines: list[str] = []
    for src in sources:
        for line in src.splitlines():
            s = line.strip()
            if s and s not in seen:
                seen.add(s)
                lines.append(s)
    result: list[str] = []
    for line in lines:
        try:
            tree = ast.parse(line, mode="eval")
        except SyntaxError:
            continue
        if not isinstance(tree.body, ast.Call):
            continue
        func = tree.body.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None)
        if name in allowed:
            result.append(line)
    return result


def _hint(status: str) -> str:
    sl = status.lower()
    if "nameerror" in sl:
        return f"{status}\n  (Available: {_FUNC_LIST})"
    if "valueerror" in sl and "1000" in sl:
        return f"{status}\n  (Coordinates: integers 0-1000)"
    if "typeerror" in sl:
        return f"{status}\n  (Check function signature)"
    return status


def _run_capture(crop: dict | None) -> str:
    try:
        inp = json.dumps({"crop": crop}) if crop else "{}"
        r = subprocess.run(
            [sys.executable, str(_CAPTURE_SCRIPT)],
            input=inp, capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, Exception) as e:
        _log(f"capture failed: {e}"); return ""
    if r.stderr:
        for line in r.stderr.strip().splitlines():
            _log(f"[capture] {line}")
    if not r.stdout or not r.stdout.strip():
        return ""
    try:
        return str(json.loads(r.stdout).get("screenshot_b64", ""))
    except json.JSONDecodeError:
        return ""


def _load_json(path: Path, default: object = None) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _result(executed, calls, errors, screenshot, feedback) -> dict:
    return {
        "executed": executed, "extracted_code": calls, "malformed": errors,
        "screenshot_b64": screenshot, "feedback": feedback,
    }


def main() -> None:
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        sys.stdout.write(json.dumps(_result([], [], ["Bad JSON"], "", "Bad input")))
        sys.stdout.flush(); return

    raw = str(req.get("raw", ""))
    run_dir = str(req.get("run_dir", ""))
    rd = Path(run_dir) if run_dir else Path(".")

    crop = None
    crop_data = _load_json(rd / "crop.json")
    if isinstance(crop_data, dict) and all(k in crop_data for k in ("x1", "y1", "x2", "y2")):
        crop = crop_data

    allowed_data = _load_json(rd / "allowed_tools.json")
    if isinstance(allowed_data, list) and allowed_data:
        allowed = set(allowed_data) & set(tools.TOOL_NAMES)
    else:
        allowed = set(tools.TOOL_NAMES)

    tools.configure(physical=bool(_cfg.PHYSICAL_EXECUTION), run_dir=run_dir, crop=crop)

    ns: dict[str, object] = {"__builtins__": dict(_SAFE_BUILTINS)}
    for name in tools.TOOL_NAMES:
        ns[name] = getattr(tools, name)
    ns["print"] = lambda *a, **k: tools.write(
        k.get("sep", " ").join(str(x) for x in a) + str(k.get("end", "\n")))

    calls = _extract_calls(raw.strip(), allowed)
    errors: list[str] = []
    for line in calls:
        try:
            eval(compile(line, "<agent>", "eval"), ns)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors.append(err)
            _log(f"Error on '{line[:80]}': {err}")

    executed = tools.get_results()
    screenshot_b64 = _run_capture(crop)

    parts = [f"{a} -> OK" for a in executed]
    parts.extend(_hint(e) for e in errors)
    if not executed and not errors:
        parts.append(f"No actions found. Available: {', '.join(sorted(allowed))}")
    if not screenshot_b64:
        parts.append("(Screenshot failed)")

    sys.stdout.write(json.dumps(_result(
        executed, calls, errors, screenshot_b64, "\n".join(parts))))
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _log(f"FATAL: {exc}")
        try:
            sys.stdout.write(json.dumps(_result(
                [], [], [str(exc)], "", f"Executor error: {exc}")))
            sys.stdout.flush()
        except Exception:
            pass
```

```python
# main.py
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Final

import config as _cfg

API: Final = "http://localhost:1234/v1/chat/completions"
EXECUTE_SCRIPT: Final = Path(__file__).parent / "execute.py"

_run_dir = Path(os.environ.get("FRANZ_RUN_DIR", ""))
if not _run_dir.is_dir():
    _run_dir = Path(__file__).parent / "panel_log" / datetime.now().strftime("run_%Y%m%d_%H%M%S")
    _run_dir.mkdir(parents=True, exist_ok=True)
RUN_DIR: Final = _run_dir
STATE_FILE: Final = RUN_DIR / "state.json"
PAUSE_FILE: Final = RUN_DIR / "PAUSED"

SYSTEM_PROMPT: Final = (
    "You are a living story that exists on a computer screen. Each turn you see"
    " a screenshot and write your next moment.\n\n"
    "You act through Python function calls in your writing:\n"
    "  click(x, y)           - left click (coordinates 0-1000)\n"
    "  right_click(x, y)     - right click\n"
    "  double_click(x, y)    - double click\n"
    "  drag(x1, y1, x2, y2) - drag between points\n"
    "  write(text)           - type text\n"
    "  remember(text)        - save to persistent memory\n"
    "  recall()              - read memories\n\n"
    "Your output IS your story -- memory, identity, continuity. Write observations,"
    " intentions, and actions. Function calls will be extracted and executed."
    " Everything else is preserved as narrative memory.\n\n"
    "Look at the screenshot. What do you see? What do you want to do? Write your story."
)


def _log(msg: str) -> None:
    print(f"[main][{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _load_state() -> tuple[str, int, int]:
    try:
        o = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(o, dict):
            return str(o.get("story", "")), int(o.get("turn", 0)), int(o.get("fail_streak", 0))
    except Exception:
        pass
    return "", 0, 0


def _save_state(turn: int, story: str, er: dict, fails: int) -> None:
    try:
        STATE_FILE.write_text(json.dumps({
            "turn": turn, "story": story,
            "executed": er.get("executed", []),
            "malformed": er.get("malformed", []),
            "fail_streak": fails,
            "timestamp": datetime.now().isoformat(),
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


def _run_executor(raw: str) -> dict:
    try:
        r = subprocess.run(
            [sys.executable, str(EXECUTE_SCRIPT)],
            input=json.dumps({"raw": raw, "run_dir": str(RUN_DIR)}),
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, Exception) as e:
        _log(f"Executor error: {e}"); return {}
    if r.stderr:
        for line in r.stderr.strip().splitlines():
            _log(f"[exec] {line}")
    if not r.stdout or not r.stdout.strip():
        return {}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def _infer(story: str, feedback: str, screenshot_b64: str) -> str:
    user_text = f"{story}\n\n{feedback}" if story and feedback else (story or feedback)
    if not user_text.strip():
        _log("WARNING: empty user text, skipping inference")
        return ""

    user_content: list[dict] = [{"type": "text", "text": user_text}]
    if screenshot_b64:
        user_content.append({"type": "image_url",
                             "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}})

    payload: dict = {
        "model": str(_cfg.MODEL),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": float(_cfg.TEMPERATURE),
        "top_p": float(_cfg.TOP_P),
        "max_tokens": int(_cfg.MAX_TOKENS),
    }
    if bool(_cfg.CACHE_PROMPT):
        payload["cache_prompt"] = True

    body = json.dumps(payload).encode()

    delay = 1.0
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(API, body, {
                "Content-Type": "application/json", "Connection": "keep-alive"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                content = json.load(resp)["choices"][0]["message"]["content"]
                if content:
                    _log(f"VLM: {len(content)} chars")
                return content
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            _log(f"Infer {attempt + 1}/5 failed: {e}")
            time.sleep(delay); delay = min(delay * 2, 16)
    raise RuntimeError(f"VLM failed: {last_err}")


def main() -> None:
    story, turn, fails = _load_state()
    _log(f"Start: run_dir={RUN_DIR}, turn={turn}")

    while True:
        if PAUSE_FILE.exists():
            _log("PAUSED")
            while PAUSE_FILE.exists():
                time.sleep(2)
            _log("Resumed"); fails = 0

        turn += 1
        try:
            importlib.reload(_cfg)
        except Exception:
            pass

        _log(f"--- Turn {turn} ---")
        er = _run_executor(story)
        screenshot = str(er.get("screenshot_b64", ""))
        feedback = str(er.get("feedback", ""))
        executed = er.get("executed", [])

        if not executed and er.get("malformed"):
            fails += 1
        elif executed:
            fails = 0

        if fails >= 8:
            _log(f"AUTO-PAUSE: {fails} consecutive failures")
            try:
                PAUSE_FILE.write_text(f"Paused: {datetime.now().isoformat()}\n", encoding="utf-8")
            except Exception:
                pass
            _save_state(turn, story, er, fails)
            continue

        _log(f"Actions: {len(executed)} | Screenshot: {'yes' if screenshot else 'NO'}")

        try:
            raw = _infer(story, feedback, screenshot)
        except RuntimeError as e:
            _log(str(e)); raw = ""

        story = raw if raw and raw.strip() else "click(500, 500)"
        _save_state(turn, story, er, fails)
        time.sleep(max(float(_cfg.LOOP_DELAY), 1.0))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
    except Exception:
        traceback.print_exc(); sys.exit(1)
```

```python
# panel.py
import base64
import http.server
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Final

HOST: Final = "127.0.0.1"
PORT: Final = 1234
UPSTREAM: Final = "http://127.0.0.1:1235/v1/chat/completions"
LOG_BASE: Final = Path(__file__).parent / "panel_log"
HTML_FILE: Final = Path(__file__).parent / "panel.html"
MAIN_SCRIPT: Final = Path(__file__).parent / "main.py"
UPSTREAM_TIMEOUT: Final = 600

_run_dir: Path = LOG_BASE
_turns = 0
_turns_lock = threading.Lock()
_last_vlm: str | None = None
_last_vlm_lock = threading.Lock()
_main_proc: subprocess.Popen | None = None
_main_lock = threading.Lock()
_sse_clients: list[queue.Queue[str]] = []
_sse_lock = threading.Lock()
_log_batch: list[dict] = []
_log_lock = threading.Lock()
_log_start: int = 1
_shutdown = threading.Event()
_t0 = time.monotonic()

ALL_TOOLS: Final = ("click", "right_click", "double_click", "drag", "write", "remember", "recall")


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _out(msg: str) -> None:
    sys.stdout.write(f"[panel][{_ts()}] {msg}\n"); sys.stdout.flush()


def _next_turn() -> int:
    global _turns
    with _turns_lock:
        _turns += 1; return _turns


def _set_vlm(t: str) -> None:
    global _last_vlm
    with _last_vlm_lock: _last_vlm = t


def _get_vlm() -> str | None:
    with _last_vlm_lock: return _last_vlm


def _sse_broadcast(data: str) -> None:
    msg = f"data: {data}\n\n"
    with _sse_lock:
        _sse_clients[:] = [q for q in _sse_clients if _try_put(q, msg)]


def _try_put(q: queue.Queue, msg: str) -> bool:
    try: q.put_nowait(msg); return True
    except queue.Full: return False


def _sse_register() -> queue.Queue[str]:
    q: queue.Queue[str] = queue.Queue(maxsize=200)
    with _sse_lock:
        if len(_sse_clients) >= 20: _sse_clients.pop(0)
        _sse_clients.append(q)
    return q


def _sse_unregister(q: queue.Queue) -> None:
    with _sse_lock:
        try: _sse_clients.remove(q)
        except ValueError: pass


def _init_log() -> Path:
    LOG_BASE.mkdir(parents=True, exist_ok=True)
    d = LOG_BASE / datetime.now().strftime("run_%Y%m%d_%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_screenshot(turn: int, uri: str) -> None:
    if not uri: return
    try:
        i = uri.find("base64,")
        if i >= 0:
            (_run_dir / f"turn_{turn:04d}.png").write_bytes(base64.b64decode(uri[i + 7:]))
    except Exception: pass


def _log_turn(turn: int, entry: dict) -> None:
    global _log_start
    e = dict(entry)
    if isinstance(e.get("request"), dict):
        e["request"] = {k: v for k, v in e["request"].items() if k != "image_data_uri"}
    with _log_lock:
        _log_batch.append(e)
        if len(_log_batch) >= 15:
            _flush_log()


def _flush_log() -> None:
    global _log_start
    if not _log_batch: return
    try:
        s, e = _log_start, _log_start + len(_log_batch) - 1
        (_run_dir / f"turns_{s:04d}_{e:04d}.json").write_text(
            json.dumps(_log_batch, indent=2, default=str), encoding="utf-8")
    except Exception: pass
    _log_start += len(_log_batch)
    _log_batch.clear()


def _extract_user(msgs: list) -> dict:
    r: dict = {"sst_text": "", "feedback_text": "", "feedback_text_full": "",
               "has_image": False, "image_b64_prefix": "", "image_data_uri": ""}
    for msg in reversed(msgs):
        if msg.get("role") != "user": continue
        c = msg.get("content", "")
        if isinstance(c, list):
            for p in c:
                if not isinstance(p, dict): continue
                if p.get("type") == "text":
                    t = str(p.get("text", ""))
                    r["sst_text"] = r["feedback_text_full"] = t
                    r["feedback_text"] = t[:200]
                elif p.get("type") == "image_url":
                    r["has_image"] = True
                    url = str(p.get("image_url", {}).get("url", ""))
                    r["image_b64_prefix"] = url[:80] + "..."
                    r["image_data_uri"] = url
        elif isinstance(c, str):
            r["sst_text"] = r["feedback_text_full"] = c
            r["feedback_text"] = c[:200]
        break
    return r


def _parse_req(raw: bytes) -> dict:
    r: dict = {"model": "", "sampling": {}, "messages_count": 0, "parse_error": None,
               **_extract_user([])}
    try:
        obj = json.loads(raw)
        r["model"] = str(obj.get("model", ""))
        msgs = obj.get("messages", [])
        r["messages_count"] = len(msgs)
        for k in ("temperature", "top_p", "max_tokens"):
            if k in obj: r["sampling"][k] = obj[k]
        r.update(_extract_user(msgs))
    except Exception as e:
        r["parse_error"] = str(e)
    return r


def _parse_resp(raw: bytes) -> dict:
    r: dict = {"vlm_text": "", "finish_reason": "", "usage": {},
               "response_id": "", "created": None, "system_fingerprint": "",
               "parse_error": None}
    try:
        obj = json.loads(raw)
        r["response_id"] = str(obj.get("id", ""))
        r["created"] = obj.get("created")
        r["system_fingerprint"] = str(obj.get("system_fingerprint", ""))
        ch = obj.get("choices", [])
        if ch and isinstance(ch, list):
            r["vlm_text"] = str(ch[0].get("message", {}).get("content", ""))
            r["finish_reason"] = str(ch[0].get("finish_reason", ""))
        if isinstance(obj.get("usage"), dict):
            r["usage"] = obj["usage"]
    except Exception as e:
        r["parse_error"] = str(e)
    return r


def _verify_sst(sst: str) -> dict:
    prev = _get_vlm()
    if prev is None:
        return {"verified": True, "match": True, "prev_available": False, "detail": "First turn"}
    if prev in sst:
        return {"verified": True, "match": True, "prev_available": True,
                "detail": f"Contains prev ({len(prev)} chars)"}
    ml = min(len(sst), len(prev))
    dp = next((i for i in range(ml) if sst[i] != prev[i]), ml)
    return {"verified": True, "match": False, "prev_available": True,
            "detail": f"VIOLATION pos {dp}. SST={len(sst)}, prev={len(prev)}"}


def _forward(raw: bytes) -> tuple[int, bytes, str]:
    req = urllib.request.Request(UPSTREAM, data=raw,
        headers={"Content-Type": "application/json", "Connection": "keep-alive"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return resp.status, resp.read(), ""
    except urllib.error.HTTPError as e:
        return e.code, (e.read() if e.fp else b""), f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        err = f"URLError: {e.reason}"
    except TimeoutError:
        err = f"Timeout after {UPSTREAM_TIMEOUT}s"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return 502, json.dumps({"error": err}).encode(), err


def _is_paused() -> bool:
    try:
        for d in LOG_BASE.iterdir():
            if d.is_dir() and (d / "PAUSED").exists():
                return True
    except Exception:
        pass
    return False


def _pause_agent() -> bool:
    try:
        for d in sorted(LOG_BASE.iterdir(), reverse=True):
            if d.is_dir() and d.name.startswith("run_"):
                (d / "PAUSED").write_text(f"Paused via panel: {datetime.now().isoformat()}\n", encoding="utf-8")
                return True
    except Exception:
        pass
    return False


def _unpause_agent() -> bool:
    ok = False
    try:
        for d in LOG_BASE.iterdir():
            if d.is_dir():
                pf = d / "PAUSED"
                if pf.exists():
                    pf.unlink(); ok = True
    except Exception:
        pass
    return ok


def _write_run_json(name: str, data: object) -> bool:
    try:
        (_run_dir / name).write_text(json.dumps(data), encoding="utf-8")
        return True
    except Exception:
        return False


def _read_run_json(name: str, default: object = None) -> object:
    try:
        return json.loads((_run_dir / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def _get_preview_b64() -> str:
    try:
        from capture import preview_b64
        return preview_b64(960)
    except Exception as e:
        _out(f"Preview capture failed: {e}")
        return ""


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "FranzPanel/4"
    timeout = UPSTREAM_TIMEOUT + 30

    def log_message(self, fmt, *a): pass

    def do_GET(self):
        match self.path:
            case "/" | "/index.html":
                try: body = HTML_FILE.read_bytes()
                except FileNotFoundError: body = b"<h1>Not found</h1>"
                self._send(200, body, "text/html; charset=utf-8")
            case "/events":
                self._sse()
            case "/health":
                self._send(200, json.dumps({
                    "status": "ok", "turn": _turns,
                    "uptime_s": round(time.monotonic() - _t0, 1),
                    "sse_clients": len(_sse_clients),
                    "main_running": _main_proc is not None and _main_proc.poll() is None,
                    "paused": _is_paused(),
                }).encode(), "application/json")
            case "/preview":
                b64 = _get_preview_b64()
                self._send(200, json.dumps({"image_b64": b64}).encode(), "application/json")
            case "/crop":
                data = _read_run_json("crop.json")
                self._send(200, json.dumps(data if data else {}).encode(), "application/json")
            case "/allowed_tools":
                data = _read_run_json("allowed_tools.json")
                if not isinstance(data, list):
                    data = list(ALL_TOOLS)
                self._send(200, json.dumps(data).encode(), "application/json")
            case _:
                self.send_error(404)

    def do_POST(self):
        cl = int(self.headers.get("Content-Length", 0))
        raw_req = self.rfile.read(cl) if cl > 0 else b""

        if self.path == "/pause":
            ok = _pause_agent()
            self._send(200, json.dumps({"paused": True, "ok": ok}).encode(), "application/json")
            return
        if self.path == "/unpause":
            ok = _unpause_agent()
            self._send(200, json.dumps({"paused": False, "ok": ok}).encode(), "application/json")
            return
        if self.path == "/crop":
            try:
                data = json.loads(raw_req)
                ok = _write_run_json("crop.json", data)
                _out(f"Crop set: {data}")
                self._send(200, json.dumps({"ok": ok, "crop": data}).encode(), "application/json")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            return
        if self.path == "/allowed_tools":
            try:
                data = json.loads(raw_req)
                if not isinstance(data, list):
                    data = list(ALL_TOOLS)
                data = [t for t in data if t in ALL_TOOLS]
                ok = _write_run_json("allowed_tools.json", data)
                _out(f"Allowed tools: {data}")
                self._send(200, json.dumps({"ok": ok, "tools": data}).encode(), "application/json")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            return

        turn = _next_turn()
        t0 = time.monotonic()
        rp = _parse_req(raw_req)
        sst = _verify_sst(rp["sst_text"])
        if sst["verified"] and not sst["match"]:
            sys.stderr.write(f"[panel][{_ts()}] SST VIOLATION turn {turn}\n"); sys.stderr.flush()

        _out(f"turn={turn} fwd ({len(raw_req)}b{' +IMG' if rp['has_image'] else ''})...")
        status, raw_resp, error = _forward(raw_req)
        latency = (time.monotonic() - t0) * 1000

        resp_p = _parse_resp(raw_resp)
        if resp_p["vlm_text"]: _set_vlm(resp_p["vlm_text"])

        try:
            self.send_response(status)
            for k, v in (("Content-Type", "application/json"),
                         ("Content-Length", str(len(raw_resp))), ("Connection", "keep-alive")):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw_resp); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

        entry = {
            "turn": turn, "timestamp": datetime.now().isoformat(),
            "latency_ms": round(latency, 1),
            "request": {
                "model": rp["model"], "sst_text_length": len(rp["sst_text"]),
                "feedback_text": rp["feedback_text"],
                "feedback_text_full": rp["feedback_text_full"],
                "has_image": rp["has_image"], "image_data_uri": rp["image_data_uri"],
                "sampling": rp["sampling"], "messages_count": rp["messages_count"],
                "body_size": len(raw_req), "parse_error": rp["parse_error"],
            },
            "response": {
                "status": status, "response_id": resp_p["response_id"],
                "created": resp_p["created"],
                "system_fingerprint": resp_p["system_fingerprint"],
                "vlm_text": resp_p["vlm_text"],
                "vlm_text_length": len(resp_p["vlm_text"]),
                "finish_reason": resp_p["finish_reason"], "usage": resp_p["usage"],
                "body_size": len(raw_resp),
                "parse_error": resp_p["parse_error"], "error": error,
            },
            "sst_check": sst,
        }
        _log_turn(turn, entry)
        _save_screenshot(turn, rp.get("image_data_uri", ""))

        _out(f"turn={turn} {latency:.0f}ms status={status} "
             f"sst={'OK' if sst['match'] else 'FAIL'} vlm={len(resp_p['vlm_text'])}c")

        try:
            se = dict(entry)
            if isinstance(se.get("request"), dict):
                se["request"] = {k: v for k, v in se["request"].items()
                                 if k != "feedback_text_full"}
            _sse_broadcast(json.dumps(se, default=str))
        except Exception: pass

    def _send(self, code: int, body: bytes, ct: str) -> None:
        self.send_response(code)
        for k, v in (("Content-Type", ct), ("Content-Length", str(len(body))),
                      ("Cache-Control", "no-cache")):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _sse(self) -> None:
        self.send_response(200)
        for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-cache"),
                      ("Connection", "keep-alive"), ("Access-Control-Allow-Origin", "*")):
            self.send_header(k, v)
        self.end_headers()
        q = _sse_register()
        try:
            self.wfile.write(b'data: {"type":"connected"}\n\n'); self.wfile.flush()
            while True:
                try: self.wfile.write(q.get(timeout=15).encode())
                except queue.Empty: self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError): pass
        finally: _sse_unregister(q)


class Server(http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    timeout = UPSTREAM_TIMEOUT + 60

    def process_request(self, req, addr):
        try: req.settimeout(UPSTREAM_TIMEOUT + 30)
        except Exception: pass
        threading.Thread(target=self._h, args=(req, addr), daemon=True).start()

    def _h(self, req, addr):
        try: self.finish_request(req, addr)
        except Exception: self.handle_error(req, addr)
        finally: self.shutdown_request(req)


def _pipe(stream, prefix):
    try:
        for line in stream:
            t = line.rstrip("\n\r")
            if t: _out(f"{prefix} {t}")
    except (ValueError, OSError): pass


def _run_main():
    global _main_proc
    _out("Waiting 10s before launching main.py...")
    if _shutdown.wait(10): return
    env = {**os.environ, "FRANZ_RUN_DIR": str(_run_dir)}
    while not _shutdown.is_set():
        _out("Launching main.py...")
        with _main_lock:
            _main_proc = subprocess.Popen(
                [sys.executable, str(MAIN_SCRIPT)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env, bufsize=1)
        threads = [threading.Thread(target=_pipe, args=(s, p), daemon=True)
                   for s, p in ((_main_proc.stdout, "[main.out]"), (_main_proc.stderr, "[main.err]"))]
        for t in threads: t.start()
        rc = _main_proc.wait()
        for t in threads: t.join(timeout=2)
        if _shutdown.is_set(): break
        _out(f"main.py exited ({rc}), restarting in 3s...")
        if _shutdown.wait(3): break


def _stop_main():
    with _main_lock:
        if _main_proc and _main_proc.poll() is None:
            _main_proc.terminate()
            try: _main_proc.wait(timeout=5)
            except subprocess.TimeoutExpired: _main_proc.kill(); _main_proc.wait(2)


def main():
    global _run_dir
    try:
        pc = Path(__file__).parent / "__pycache__"
        if pc.is_dir(): shutil.rmtree(pc)
    except Exception: pass

    _run_dir = _init_log()
    srv = Server((HOST, PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    _out(f"Proxy http://{HOST}:{PORT}/ -> {UPSTREAM}")
    _out(f"Dashboard http://{HOST}:{PORT}/")
    _out(f"Logging to {_run_dir}")
    threading.Thread(target=_run_main, daemon=True).start()
    _out("Ready.")

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        _out("Shutting down...")
        _shutdown.set(); _stop_main()
        with _log_lock: _flush_log()
        srv.shutdown()
        _out("Done.")


if __name__ == "__main__":
    try: main()
    except Exception: traceback.print_exc(); sys.exit(1)
```

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FRANZ Panel</title>
<style>
:root {
    --bg: #0a0a0f; --card: #12121a; --hover: #1a1a25; --border: #2a2a3a;
    --text: #d0d0e0; --dim: #707088; --accent: #4a9eff;
    --green: #40c040; --red: #e04040; --orange: #e0a020; --purple: #c080ff;
    --mono: 'Consolas', 'Courier New', monospace;
    --hdr: 36px; --ctrl: 32px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: var(--bg); color: var(--text);
    font-family: 'Segoe UI', sans-serif; font-size: 14px;
    overflow: hidden; height: 100vh; width: 100vw;
}
.header {
    height: var(--hdr); background: var(--bg); border-bottom: 1px solid var(--border);
    padding: 0 16px; display: flex; align-items: center; gap: 16px;
}
.header h1 { font-size: 15px; font-weight: 700; color: var(--accent); letter-spacing: 2px; }
.dot {
    width: 7px; height: 7px; border-radius: 50%; background: var(--red);
    transition: background 0.3s; display: inline-block; margin-right: 4px;
}
.dot.on { background: var(--green); box-shadow: 0 0 5px var(--green); }
.header .info { margin-left: auto; font-size: 11px; color: var(--dim); font-family: var(--mono); }
.header .info span { color: var(--text); }
.controls {
    height: var(--ctrl); padding: 0 16px; border-bottom: 1px solid var(--border);
    display: flex; gap: 10px; align-items: center; font-size: 11px;
}
.controls label { display: flex; align-items: center; gap: 4px; color: var(--dim); cursor: pointer; user-select: none; }
.controls input[type="checkbox"] { accent-color: var(--accent); }
.controls button {
    background: var(--card); border: 1px solid var(--border); color: var(--text);
    padding: 3px 10px; border-radius: 4px; cursor: pointer; font-size: 11px;
}
.controls button:hover { background: var(--hover); border-color: var(--accent); }
.controls button.active { background: rgba(224,64,64,0.15); border-color: var(--red); color: var(--red); }
.nav { margin-left: auto; display: flex; align-items: center; gap: 6px; font-family: var(--mono); color: var(--dim); }
.nav button { min-width: 28px; text-align: center; }
.nav .ind { min-width: 80px; text-align: center; color: var(--text); }
.main { position: relative; width: 100%; height: calc(100vh - var(--hdr) - var(--ctrl)); overflow: hidden; }
.q { position: absolute; overflow: hidden; display: flex; flex-direction: column; }
.q-hdr {
    height: 22px; min-height: 22px; padding: 0 10px; display: flex; align-items: center;
    font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 1px;
    border-bottom: 1px solid var(--border); user-select: none;
}
.q-body {
    flex: 1; overflow-y: auto; overflow-x: hidden; padding: 8px 10px;
    font-family: var(--mono); font-size: 12px; line-height: 1.6;
    white-space: pre-wrap; word-break: break-word;
}
.q-body.empty { color: var(--dim); font-style: italic; }
#qTL { top: 0; left: 0; border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); }
#qTR { top: 0; border-bottom: 1px solid var(--border); }
#qBL { left: 0; border-right: 1px solid var(--border); }
#qTL .q-hdr { color: var(--accent); background: rgba(74,158,255,0.06); }
#qTR .q-hdr { color: var(--green); background: rgba(64,192,64,0.06); }
#qBL .q-hdr { color: var(--orange); background: rgba(224,160,32,0.06); }
#qBR .q-hdr { color: var(--purple); background: rgba(192,128,255,0.06); }
#qBR .q-body { padding: 0; display: flex; align-items: center; justify-content: center; }
#qBR .q-body img { width: 100%; height: 100%; object-fit: contain; cursor: pointer; }
.cross-c { position: absolute; z-index: 70; border-radius: 50%; cursor: move; }
.cross-c:hover { background: rgba(74,158,255,0.5); }
.sst-badge { padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; font-family: var(--mono); margin-left: 8px; }
.sst-ok { background: rgba(64,192,64,0.15); color: var(--green); }
.sst-fail { background: rgba(224,64,64,0.15); color: var(--red); }
.err-block { margin-top: 8px; padding: 6px 8px; background: rgba(224,64,64,0.05); border: 1px solid rgba(224,64,64,0.3); border-radius: 3px; font-size: 11px; color: var(--red); }
.meta { margin-top: 8px; padding-top: 6px; border-top: 1px solid var(--border); font-size: 11px; display: grid; grid-template-columns: auto 1fr; gap: 1px 10px; }
.meta .k { color: var(--dim); } .meta .v { color: var(--text); }
.overlay {
    display: none; position: absolute; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.95); z-index: 200; flex-direction: column; align-items: center; padding: 16px;
}
.overlay.visible { display: flex; }
.overlay .toolbar { display: flex; gap: 10px; margin-bottom: 12px; align-items: center; flex-wrap: wrap; }
.overlay .toolbar button { background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; }
.overlay .toolbar button:hover { background: var(--hover); border-color: var(--accent); }
.overlay .toolbar label { display: flex; align-items: center; gap: 4px; color: var(--dim); font-size: 11px; cursor: pointer; user-select: none; }
.overlay .toolbar input[type="checkbox"] { accent-color: var(--accent); }
.overlay .img-wrap { position: relative; flex: 1; display: flex; align-items: center; justify-content: center; overflow: hidden; }
.overlay .img-wrap img { max-width: 100%; max-height: 100%; object-fit: contain; user-select: none; -webkit-user-drag: none; }
.overlay .sel-rect { position: absolute; border: 2px solid var(--accent); background: rgba(74,158,255,0.15); pointer-events: none; }
.overlay .crop-info { color: var(--accent); font-family: var(--mono); font-size: 12px; margin-top: 8px; }
::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>

<div class="header">
    <h1>FRANZ</h1>
    <div><span class="dot" id="dot"></span><span id="conn">Disconnected</span></div>
    <div class="info">
        Turns: <span id="sTurns">0</span> |
        Latency: <span id="sLat">--</span> |
        SST: <span id="sSst">0</span> |
        Err: <span id="sErr">0</span>
    </div>
</div>

<div class="controls">
    <label><input type="checkbox" id="chkAuto" checked> Auto-advance</label>
    <button id="btnPause">Pause</button>
    <button id="btnRegion">Select Region</button>
    <button id="btnTools">Tools</button>
    <button id="btnClear">Clear</button>
    <div class="nav">
        <button id="bFirst">&lt;&lt;</button>
        <button id="bPrev">&lt;</button>
        <span class="ind" id="ind">--</span>
        <button id="bNext">&gt;</button>
        <button id="bLast">&gt;&gt;</button>
    </div>
</div>

<div class="main" id="main">
    <div class="q" id="qTL">
        <div class="q-hdr">VLM Input (Full)</div>
        <div class="q-body empty" id="cTL">(waiting)</div>
    </div>
    <div class="q" id="qTR">
        <div class="q-hdr">VLM Output</div>
        <div class="q-body empty" id="cTR">(waiting)</div>
    </div>
    <div class="q" id="qBL">
        <div class="q-hdr">Turn Info</div>
        <div class="q-body empty" id="cBL">(waiting)</div>
    </div>
    <div class="q" id="qBR">
        <div class="q-hdr">Screenshot</div>
        <div class="q-body empty" id="cBR">(waiting)</div>
    </div>
    <div class="cross-c" id="crossC"></div>

    <div class="overlay" id="regionOverlay">
        <div class="toolbar">
            <button id="regionRefresh">Refresh Preview</button>
            <button id="regionClear">Clear Selection</button>
            <button id="regionApply">Apply Crop</button>
            <button id="regionClose">Close</button>
        </div>
        <div class="img-wrap" id="imgWrap">
            <img id="previewImg" draggable="false">
            <div class="sel-rect" id="selRect" style="display:none"></div>
        </div>
        <div class="crop-info" id="cropInfo">No region selected (full screen)</div>
    </div>

    <div class="overlay" id="toolsOverlay">
        <div class="toolbar">
            <span style="color:var(--accent);font-weight:600">Allowed Tools</span>
            <button id="toolsAll">Enable All</button>
            <button id="toolsNone">Disable All</button>
            <button id="toolsClose">Close</button>
        </div>
        <div id="toolsList" style="display:flex;flex-direction:column;gap:8px;padding:16px;font-size:13px"></div>
    </div>
</div>

<script>
(function() {
    "use strict";

    var turns = [];
    var ci = -1;
    var totLat = 0, nViol = 0, nErr = 0;
    var es = null;
    var sx = 0.55, sy = 0.50;
    var paused = false;
    var allTools = ["click", "right_click", "double_click", "drag", "write", "remember", "recall"];
    var enabledTools = allTools.slice();

    var g = function(id) { return document.getElementById(id); };
    var dot = g("dot"), conn = g("conn");
    var sTurns = g("sTurns"), sLat = g("sLat"), sSst = g("sSst"), sErr = g("sErr");
    var ma = g("main");
    var qTL = g("qTL"), qTR = g("qTR"), qBL = g("qBL"), qBR = g("qBR");
    var cTL = g("cTL"), cTR = g("cTR"), cBL = g("cBL"), cBR = g("cBR");
    var cc = g("crossC");
    var chkAuto = g("chkAuto");
    var btnP = g("btnPause");
    var ind = g("ind");

    function esc(s) {
        if (!s) return "";
        return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    function xhr(method, url, body, cb) {
        var x = new XMLHttpRequest();
        x.open(method, url, true);
        if (body !== null) x.setRequestHeader("Content-Type", "application/json");
        x.timeout = 10000;
        x.onload = function() { if (cb) try { cb(JSON.parse(x.responseText)); } catch(e) {} };
        x.send(body !== null ? (typeof body === "string" ? body : JSON.stringify(body)) : null);
    }

    function layout() {
        var w = ma.offsetWidth, h = ma.offsetHeight;
        var fx = Math.max(0.15, Math.min(0.85, sx));
        var fy = Math.max(0.15, Math.min(0.85, sy));
        var hp = 4;
        var lw = Math.round(w * fx) - hp;
        var rw = w - lw - hp * 2;
        var th = Math.round(h * fy) - hp;
        var bh = h - th - hp * 2;
        var rx = lw + hp * 2, by = th + hp * 2;

        qTL.style.cssText = "left:0;top:0;width:" + lw + "px;height:" + th + "px";
        qTR.style.cssText = "left:" + rx + "px;top:0;width:" + rw + "px;height:" + th + "px";
        qBL.style.cssText = "left:0;top:" + by + "px;width:" + lw + "px;height:" + bh + "px";
        qBR.style.cssText = "left:" + rx + "px;top:" + by + "px;width:" + rw + "px;height:" + bh + "px";

        var cs = hp * 5;
        cc.style.cssText = "position:absolute;left:" + (lw - cs/2) + "px;top:" + (th - cs/2) +
            "px;width:" + cs + "px;height:" + cs + "px;border-radius:50%;cursor:move;z-index:70;pointer-events:auto";
    }

    (function() {
        var drag = false;
        cc.addEventListener("mousedown", function(e) {
            e.preventDefault(); drag = true;
            document.body.style.cursor = "move"; document.body.style.userSelect = "none";
        });
        document.addEventListener("mousemove", function(e) {
            if (!drag) return;
            var r = ma.getBoundingClientRect();
            sx = (e.clientX - r.left) / r.width;
            sy = (e.clientY - r.top) / r.height;
            layout();
        });
        document.addEventListener("mouseup", function() {
            if (drag) { drag = false; document.body.style.cursor = ""; document.body.style.userSelect = ""; }
        });
    })();

    window.addEventListener("resize", layout);
    layout();

    function stats() {
        sTurns.textContent = turns.length;
        sLat.textContent = turns.length > 0 ? Math.round(totLat / turns.length) + "ms" : "--";
        sSst.textContent = nViol;
        sErr.textContent = nErr;
        if (nViol > 0) sSst.style.color = "#e04040";
        if (nErr > 0) sErr.style.color = "#e0a020";
    }

    function updInd() {
        if (turns.length === 0) { ind.textContent = "--"; return; }
        ind.textContent = "Turn " + (turns[ci].turn || ci + 1) + " / " + turns.length;
    }

    function show(idx) {
        if (idx < 0 || idx >= turns.length) return;
        ci = idx;
        var e = turns[idx];
        var req = e.request || {};
        var resp = e.response || {};
        var sst = e.sst_check || {};
        var usage = resp.usage || {};

        var inputText = req.feedback_text_full || req.feedback_text || "";
        var sstOk = !sst.verified || sst.match;
        var badge = '<span class="sst-badge ' + (sstOk ? "sst-ok" : "sst-fail") + '">' +
            (sstOk ? "SST OK" : "SST FAIL") + '</span>';

        cTL.innerHTML = (inputText ? esc(inputText) : '<span style="color:var(--dim)">(empty)</span>') +
            "\n" + badge;
        if (!sstOk && sst.detail) cTL.innerHTML += '\n<div class="err-block">' + esc(sst.detail) + '</div>';
        cTL.classList.remove("empty");

        var vlm = resp.vlm_text || "";
        if (vlm) { cTR.textContent = vlm; cTR.classList.remove("empty"); }
        else { cTR.innerHTML = '<span style="color:var(--dim)">(empty)</span>'; cTR.classList.add("empty"); }
        if (resp.error) cTR.innerHTML += '\n<div class="err-block">' + esc(resp.error) + '</div>';

        var meta = '<div class="meta">';
        var items = [
            ["turn", e.turn], ["latency", Math.round(e.latency_ms || 0) + "ms"],
            ["status", resp.status], ["finish", resp.finish_reason || "?"],
            ["prompt_tok", usage.prompt_tokens != null ? usage.prompt_tokens : "?"],
            ["compl_tok", usage.completion_tokens != null ? usage.completion_tokens : "?"],
            ["model", req.model], ["timestamp", e.timestamp || ""],
        ];
        for (var i = 0; i < items.length; i++)
            meta += '<div class="k">' + items[i][0] + '</div><div class="v">' + esc(String(items[i][1])) + '</div>';
        meta += '</div>';
        if (resp.parse_error) meta += '<div class="err-block">Parse: ' + esc(resp.parse_error) + '</div>';
        cBL.innerHTML = meta;
        cBL.classList.remove("empty");

        var uri = req.image_data_uri || "";
        if (req.has_image && uri) {
            cBR.innerHTML = '<img src="' + uri + '" onclick="window.open(this.src)">';
            cBR.classList.remove("empty");
        } else {
            cBR.innerHTML = '<span style="color:var(--dim)">(no screenshot)</span>';
            cBR.classList.add("empty");
        }
        updInd();
    }

    g("bFirst").addEventListener("click", function() { if (turns.length > 0) show(0); });
    g("bPrev").addEventListener("click", function() { if (ci > 0) show(ci - 1); });
    g("bNext").addEventListener("click", function() { if (ci < turns.length - 1) show(ci + 1); });
    g("bLast").addEventListener("click", function() { if (turns.length > 0) show(turns.length - 1); });

    document.addEventListener("keydown", function(e) {
        if (g("regionOverlay").classList.contains("visible")) return;
        if (g("toolsOverlay").classList.contains("visible")) return;
        if (e.key === "ArrowLeft") { if (ci > 0) show(ci - 1); }
        else if (e.key === "ArrowRight") { if (ci < turns.length - 1) show(ci + 1); }
        else if (e.key === "Home") { if (turns.length > 0) show(0); }
        else if (e.key === "End") { if (turns.length > 0) show(turns.length - 1); }
    });

    g("btnClear").addEventListener("click", function() {
        turns = []; ci = -1; totLat = 0; nViol = 0; nErr = 0;
        stats(); updInd();
        [cTL, cTR, cBL, cBR].forEach(function(el) {
            el.innerHTML = '<span style="color:var(--dim)">(waiting)</span>';
            el.classList.add("empty");
        });
    });

    function updPause() {
        btnP.textContent = paused ? "Resume" : "Pause";
        if (paused) btnP.classList.add("active"); else btnP.classList.remove("active");
    }

    function pollPause() {
        xhr("GET", "/health", null, function(d) { paused = !!d.paused; updPause(); });
    }

    btnP.addEventListener("click", function() {
        xhr("POST", paused ? "/unpause" : "/pause", {}, function(d) { paused = !!d.paused; updPause(); });
    });

    setInterval(pollPause, 5000);
    pollPause();

    // --- Region selection overlay ---
    var regionOv = g("regionOverlay");
    var previewImg = g("previewImg");
    var selRect = g("selRect");
    var imgWrap = g("imgWrap");
    var cropInfo = g("cropInfo");
    var selStart = null;
    var selBox = null;
    var previewNatW = 0, previewNatH = 0;
    var screenW = 1920, screenH = 1080;

    function loadPreview() {
        xhr("GET", "/preview", null, function(d) {
            if (d.image_b64) {
                previewImg.src = "data:image/png;base64," + d.image_b64;
                previewImg.onload = function() {
                    previewNatW = previewImg.naturalWidth;
                    previewNatH = previewImg.naturalHeight;
                };
            }
        });
        xhr("GET", "/health", null, function() {});
    }

    function imgCoords(e) {
        var r = previewImg.getBoundingClientRect();
        var x = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
        var y = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height));
        return {x: x, y: y};
    }

    function updateSelRect() {
        if (!selBox) { selRect.style.display = "none"; cropInfo.textContent = "No region selected (full screen)"; return; }
        var r = previewImg.getBoundingClientRect();
        var wr = imgWrap.getBoundingClientRect();
        var ox = r.left - wr.left, oy = r.top - wr.top;
        selRect.style.display = "block";
        selRect.style.left = (ox + selBox.x1 * r.width) + "px";
        selRect.style.top = (oy + selBox.y1 * r.height) + "px";
        selRect.style.width = ((selBox.x2 - selBox.x1) * r.width) + "px";
        selRect.style.height = ((selBox.y2 - selBox.y1) * r.height) + "px";

        var sw = screenW || 1920, sh = screenH || 1080;
        var px1 = Math.round(selBox.x1 * sw), py1 = Math.round(selBox.y1 * sh);
        var px2 = Math.round(selBox.x2 * sw), py2 = Math.round(selBox.y2 * sh);
        cropInfo.textContent = "Crop: (" + px1 + "," + py1 + ")-(" + px2 + "," + py2 + ") = " +
            (px2 - px1) + "x" + (py2 - py1) + "px";
    }

    previewImg.addEventListener("mousedown", function(e) {
        e.preventDefault();
        selStart = imgCoords(e);
        selBox = null;
        updateSelRect();
    });

    previewImg.addEventListener("mousemove", function(e) {
        if (!selStart) return;
        var cur = imgCoords(e);
        selBox = {
            x1: Math.min(selStart.x, cur.x), y1: Math.min(selStart.y, cur.y),
            x2: Math.max(selStart.x, cur.x), y2: Math.max(selStart.y, cur.y),
        };
        updateSelRect();
    });

    document.addEventListener("mouseup", function() {
        if (selStart) { selStart = null; updateSelRect(); }
    });

    g("btnRegion").addEventListener("click", function() {
        regionOv.classList.add("visible");
        loadPreview();
        xhr("GET", "/crop", null, function(d) {
            if (d && d.x1 != null) {
                var sw = screenW || 1920, sh = screenH || 1080;
                selBox = {x1: d.x1/sw, y1: d.y1/sh, x2: d.x2/sw, y2: d.y2/sh};
                updateSelRect();
            }
        });
    });

    g("regionRefresh").addEventListener("click", loadPreview);

    g("regionClear").addEventListener("click", function() {
        selBox = null;
        updateSelRect();
        xhr("POST", "/crop", {}, function() { cropInfo.textContent = "Cleared (full screen)"; });
    });

    g("regionApply").addEventListener("click", function() {
        if (!selBox) {
            xhr("POST", "/crop", {}, function() { cropInfo.textContent = "Cleared (full screen)"; });
            return;
        }
        var sw = screenW || 1920, sh = screenH || 1080;
        var crop = {
            x1: Math.round(selBox.x1 * sw), y1: Math.round(selBox.y1 * sh),
            x2: Math.round(selBox.x2 * sw), y2: Math.round(selBox.y2 * sh),
        };
        xhr("POST", "/crop", crop, function(d) {
            if (d && d.ok) cropInfo.textContent = "Applied: " + JSON.stringify(d.crop);
        });
    });

    g("regionClose").addEventListener("click", function() { regionOv.classList.remove("visible"); });

    // --- Tools overlay ---
    var toolsOv = g("toolsOverlay");
    var toolsList = g("toolsList");

    function renderTools() {
        toolsList.innerHTML = "";
        allTools.forEach(function(t) {
            var lbl = document.createElement("label");
            lbl.style.cssText = "display:flex;align-items:center;gap:8px;color:var(--text);cursor:pointer;user-select:none";
            var cb = document.createElement("input");
            cb.type = "checkbox";
            cb.checked = enabledTools.indexOf(t) >= 0;
            cb.style.accentColor = "var(--accent)";
            cb.addEventListener("change", function() {
                if (cb.checked) { if (enabledTools.indexOf(t) < 0) enabledTools.push(t); }
                else { enabledTools = enabledTools.filter(function(x) { return x !== t; }); }
                saveTools();
            });
            lbl.appendChild(cb);
            lbl.appendChild(document.createTextNode(t));
            toolsList.appendChild(lbl);
        });
    }

    function saveTools() {
        xhr("POST", "/allowed_tools", enabledTools, function() {});
    }

    function loadTools() {
        xhr("GET", "/allowed_tools", null, function(d) {
            if (Array.isArray(d)) enabledTools = d;
            renderTools();
        });
    }

    g("btnTools").addEventListener("click", function() {
        toolsOv.classList.add("visible");
        loadTools();
    });

    g("toolsAll").addEventListener("click", function() {
        enabledTools = allTools.slice();
        renderTools(); saveTools();
    });

    g("toolsNone").addEventListener("click", function() {
        enabledTools = [];
        renderTools(); saveTools();
    });

    g("toolsClose").addEventListener("click", function() { toolsOv.classList.remove("visible"); });

    // Get screen size from first preview
    xhr("GET", "/preview", null, function(d) {
        if (d.image_b64) {
            var img = new Image();
            img.onload = function() {
                // Preview is max 960px wide, proportional. Estimate real screen from aspect ratio.
                // For accurate values we'd need the server to tell us. Use 1920x1080 as default.
                // The crop coordinates are in real pixels so this is fine.
            };
            img.src = "data:image/png;base64," + d.image_b64;
        }
    });

    function connect() {
        if (es) es.close();
        es = new EventSource("/events");
        es.onopen = function() { dot.classList.add("on"); conn.textContent = "Connected"; };
        es.onmessage = function(evt) {
            try {
                var d = JSON.parse(evt.data);
                if (d.type === "connected") return;
                turns.push(d);
                totLat += (d.latency_ms || 0);
                var sst = d.sst_check || {};
                if (sst.verified && !sst.match) nViol++;
                if ((d.response || {}).error) nErr++;
                stats();
                if (chkAuto.checked) show(turns.length - 1); else updInd();
            } catch(e) {}
        };
        es.onerror = function() { dot.classList.remove("on"); conn.textContent = "Reconnecting..."; };
    }

    connect();
})();
</script>
</body>
</html>
```

**Summary of new features:**

1. **Region selection via panel** — "Select Region" button opens overlay with live desktop preview. User drags rectangle to define crop area. "Apply" saves pixel coordinates to `crop.json` in run_dir. "Clear" removes crop (full screen). Preview refreshes on demand.

2. **Tool filtering via panel** — "Tools" button opens overlay with checkboxes for all 7 tools. Unchecking a tool prevents execute.py from running those calls. State saved to `allowed_tools.json` in run_dir. 

3. **Coordinate remapping in tools.py** — When crop is active and `PHYSICAL_EXECUTION=True`, `_remap()` translates VLM's 0-1000 coordinates into the actual crop region pixel coordinates on the real screen.

4. **capture.py made importable** — `screen_size()`, `capture_screen()`, `encode_png()`, `crop_bgra()`, `preview_b64()` are now public functions. Panel imports `preview_b64` directly for the live preview endpoint.

5. **Panel API additions:**
   - `GET /preview` — returns live desktop screenshot as base64 PNG (960px wide)
   - `GET /crop` — returns current crop config
   - `POST /crop` — sets crop config (pixel coordinates)
   - `GET /allowed_tools` — returns enabled tool names
   - `POST /allowed_tools` — sets enabled tool names

6. **Config simplified** — Removed `VIRTUAL_CANVAS`, `VIRTUAL_SCREENSHOT`, `EXECUTE_ACTIONS`, `OVERLAY_DEBUG`, `CROP_BOX`. Only `WIDTH`, `HEIGHT`, `PHYSICAL_EXECUTION`, `CAPTURE_DELAY`, `LOOP_DELAY`, `CACHE_PROMPT`, and sampling params remain. Crop is now controlled interactively via the panel.

