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
