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
