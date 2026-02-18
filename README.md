

# Code Review: Data Flow, Architecture, and Proposed Changes

---

## Current Architecture Summary

The system is a loop:

1. **panel.py** — HTTP proxy/dashboard on `:1234`, forwards VLM requests to upstream `:1235`, logs everything, serves SSE to the browser, manages pause/crop/tools state via JSON files in `run_dir`.
2. **main.py** — Agent loop. Each turn: calls `execute.py` as a subprocess (which runs actions + captures screenshot), then calls VLM via `panel.py` proxy, stores story in `state.json`.
3. **execute.py** — Subprocess. Parses VLM output for function calls, executes them via `tools.py`, then calls `capture.py` as another subprocess for screenshot.
4. **capture.py** — Subprocess (or importable). GDI-based screen capture, crop, resize, PNG encode.
5. **tools.py** — Library loaded by `execute.py`. Win32 `SendInput` for mouse/keyboard. Coordinate remapping when crop is active.
6. **config.py** — Flat module of constants, hot-reloaded each turn by `main.py`.

Data flow per turn:
```
main.py  ──stdin/json──>  execute.py  ──stdin/json──>  capture.py
                              │                             │
                              │ (imports tools.py)          │ (returns b64 PNG)
                              │                             │
                              <─────── stdout/json ─────────┘
         <──── stdout/json ───┘
         ──── HTTP POST ────> panel.py ──── HTTP POST ────> LM Studio (:1235)
         <─── HTTP resp ─────            <─── HTTP resp ───
```

State files in `run_dir`:
- `state.json` — story, turn, fail_streak
- `crop.json` — crop rectangle
- `allowed_tools.json` — enabled tool names
- `PAUSED` — sentinel file
- `memory.json` — VLM persistent memory
- `turns_NNNN_NNNN.json` — log batches
- `turn_NNNN.png` — screenshots

---

## Data Flow Issues and Observations

### 1. Subprocess chain depth is excessive

Every turn spawns `execute.py` which spawns `capture.py`. That is two process creations per turn. On Windows, `subprocess.Popen` + Python startup is ~200-400ms each. You pay ~400-800ms per turn just in process overhead, plus JSON serialization through stdin/stdout pipes.

Given your stated constraint that the VLM takes tens of seconds, this overhead is tolerable. But it is still worth noting: `capture.py` being a separate subprocess from `execute.py` exists solely because of GDI state isolation. Since `tools.py` already calls `_init_win32()` and sets DPI awareness in the same process, and `capture.py` also sets DPI awareness at module level, there is a potential conflict if they ever run in the same process. The current subprocess isolation avoids this. This is a sound decision for now.

### 2. File-based IPC for crop/tools is correct but fragile

`crop.json` and `allowed_tools.json` are read by `execute.py` at the start of each turn. `panel.py` writes them on HTTP POST. There is no locking. If the panel writes while execute reads, you get a torn read.

In practice this is unlikely because the turn cycle is seconds long and writes are atomic on most filesystems for small files (Python's `Path.write_text` does open-write-close). On NTFS with small files this is effectively atomic. So this is acceptable, but the risk exists.

### 3. SST verification logic

`_verify_sst` checks if the previous VLM output is contained verbatim in the current SST text (the user message text sent to the VLM). This is the Single Source of Truth check — ensuring `main.py` feeds back the entire previous VLM response as part of the next prompt.

Looking at `main.py`:
```python
story = raw if raw and raw.strip() else "click(500, 500)"
```
Then next turn:
```python
user_text = f"{story}\n\n{feedback}" if story and feedback else (story or feedback)
```

So the SST text is `"{previous_vlm_output}\n\n{feedback}"`. The check `if prev in sst` works because `prev` (the previous VLM output) is a substring of this concatenation. This is correct.

However, `_extract_user` walks messages in reverse and takes the first `role=user` message. Since `main.py` sends exactly one system + one user message, this is fine. But if the message structure ever changes, this breaks silently.

### 4. Screenshot data duplication

The full base64 screenshot travels: `capture.py` stdout → `execute.py` stdout → `main.py` → HTTP POST body → `panel.py` → stored in log entry → broadcast via SSE → browser. A 1920x1080 PNG at base64 is roughly 2-5MB. This is sent through:
- Two subprocess pipes (capture→execute, execute→main)
- One HTTP request body (main→panel)
- One HTTP request body (panel→upstream)
- SSE broadcast to browser
- Saved to disk as PNG

That is 5-6 copies of ~3MB. You said this is acceptable given timing constraints. Fine. But the SSE broadcast includes `image_data_uri` in the event — actually no, looking at the SSE broadcast:

```python
se = dict(entry)
if isinstance(se.get("request"), dict):
    se["request"] = {k: v for k, v in se["request"].items()
                     if k != "feedback_text_full"}
```

It strips `feedback_text_full` but keeps `image_data_uri`. So every SSE event pushes ~3MB to each connected browser tab. With `maxsize=200` on the queue and up to 20 clients, you could have 20 × 200 × 3MB = 12GB of queued data in the worst case. The `_try_put` with `put_nowait` drops events on full queues (disconnecting that client), which mitigates this. But this is still a design smell.

**Recommendation**: Strip `image_data_uri` from the SSE event as well. The browser already has the image from the turn display. If you want the browser to always show images, have it fetch them separately (e.g., `GET /screenshot/{turn}`).

### 5. Log batching strips image but keeps feedback_text_full

In `_log_turn`:
```python
e["request"] = {k: v for k, v in e["request"].items() if k != "image_data_uri"}
```
Good — images don't go to disk logs. But `feedback_text_full` (the entire story text) is kept. As stories grow, log files grow proportionally. This is fine for debugging but worth noting.

---

## Review of Proposed Ideas

### A. Region selection during pause — modifying region each pause

**Current state**: Already implemented. The "Select Region" button is always available, not just during pause. The overlay opens, user draws rectangle, applies it, crop.json is written.

**Your TODO**: "EACH PAUSE IT SHOULD BE POSSIBLE TO MODIFY THE REGION"

This already works. The panel UI allows opening the region selector at any time, paused or not. The crop.json is read fresh each turn by execute.py. If you pause, change the region, and unpause, the next turn picks up the new crop.

**Verdict**: Already implemented. No change needed. If you want the region overlay to auto-open on pause, that is a minor UI change in panel.html (listen for pause state change, auto-open overlay). Worth doing only if you find yourself forgetting to open it manually.

### B. Live-updating preview

**Your TODO**: "THIS PREVIEW DOES NOT UPDATE IN REAL TIME"

Currently, `/preview` is fetched once when the overlay opens, and on manual "Refresh" click. To make it live, you would need either:

1. **Polling**: `setInterval` in JS fetching `/preview` every N seconds. A 960px-wide PNG is ~200-500KB base64. At 1 fetch/sec, that is manageable bandwidth-wise but creates GDI capture load continuously.
2. **Server-push**: Stream MJPEG or use a separate SSE channel for preview frames.

**Verdict**: Polling at 2-3 second intervals is the simplest and most appropriate approach. The preview overlay is open only briefly while the user is adjusting the crop. Add a `setInterval` when the overlay opens, clear it when it closes. This is a small JS change. Worth implementing.

```javascript
var previewInterval = null;
// On overlay open:
previewInterval = setInterval(loadPreview, 3000);
// On overlay close:
clearInterval(previewInterval); previewInterval = null;
```

### C. Persistence class replacing file-based state

**Your proposal**: A `Persistence` class that holds all state in memory and manages serialization.

**Current state**: State is spread across:
- `state.json` (main.py writes: story, turn, fail_streak)
- `crop.json` (panel.py writes, execute.py reads)
- `allowed_tools.json` (panel.py writes, execute.py reads)
- `PAUSED` (panel.py creates/deletes, main.py checks)
- `memory.json` (tools.py reads/writes)
- Log batches (panel.py writes)

The problem with a centralized in-memory `Persistence` class: **the system is multi-process**. `panel.py`, `main.py`, `execute.py`, and `capture.py` all run as separate processes. A class instance in one process is invisible to others. You would need either:
1. Shared memory / mmap
2. An IPC mechanism (sockets, pipes)
3. One process owns the state, others query it via API

Option 3 is the cleanest. `panel.py` is already an HTTP server. You could make it the single state owner: `main.py` reads/writes state via HTTP to `panel.py`, `execute.py` reads crop/tools via HTTP.

**But**: This introduces HTTP round-trips into the hot path. Currently `execute.py` reads `crop.json` and `allowed_tools.json` from disk (microseconds). Replacing that with HTTP calls adds milliseconds and failure modes.

**Verdict**: The file-based approach is the right one for cross-process state in this architecture. The files are small, reads are fast, and the per-turn subprocess model means each process gets a consistent snapshot at startup. A `Persistence` class would only make sense if you collapse the multi-process architecture into a single process — which would mean importing `capture.py` and `tools.py` directly into `main.py` and running everything in-thread. That is a larger refactor.

If you want cleaner code organization without changing the architecture, a `StateFiles` utility class that centralizes all the `read_json`/`write_json` calls is reasonable, but it is just a cosmetic wrapper around what you already have.

**Not recommended as described.** The current file-based IPC is appropriate for the subprocess architecture.

### D. Dynamic tool switching verification

**Your TODO**: "UNKNOWN IF SWITCHING OF TOOLS IS REALLY DYNAMIC after FIRST SWITCH"

Looking at the flow:
1. User toggles tool checkbox in panel → POST `/allowed_tools` → `_write_run_json("allowed_tools.json", data)` writes to `_run_dir / "allowed_tools.json"`.
2. Next turn, `main.py` calls `execute.py` as subprocess.
3. `execute.py` reads: `_load_json(rd / "allowed_tools.json")` → builds `allowed` set → passes to `_extract_calls(raw, allowed)`.
4. `_extract_calls` only returns lines whose function name is in `allowed`.

This is correct and fully dynamic. Every turn, `execute.py` reads the file fresh (it is a new subprocess each time). Changes take effect on the very next turn.

**Verdict**: It works. No change needed. You can verify by unchecking a tool, waiting one turn, and checking the feedback — the tool's calls will not appear in `executed`.

### E. Crop → Resize correctness

**Your TODO**: "ENSURE THE RESIZING WORKS CORRECTLY"

The flow in `capture.py`:
1. Capture full screen (`sw × sh`).
2. If crop is active: `crop_bgra()` extracts the rectangle → new dimensions `bw × bh`.
3. If `WIDTH`/`HEIGHT` are set in config: `_resize_bgra()` via GDI `StretchBlt` with `HALFTONE` mode.
4. If `WIDTH`/`HEIGHT` are 0: use crop dimensions as-is.

`crop_bgra` is a pure byte-slice operation. It correctly computes source and destination offsets with `ss = sw * 4` (source stride) and `ds = cw * 4` (dest stride). The row copy `out[do:do+ds] = bgra[so:so+ds]` is correct for top-down BGRA (negative `biHeight` in the DIB header ensures top-down layout).

`_resize_bgra` creates two DIB sections, copies source bytes via `memmove`, then uses `StretchBlt` with `HALFTONE` quality. This is correct GDI usage.

One potential issue: if the crop region is very small (e.g., 50×50 pixels) and `WIDTH`/`HEIGHT` are 0, the VLM receives a tiny image. This is by design (user chose that region), but worth noting for the future "zoom" feature you mentioned.

**Verdict**: The resize/crop pipeline is correct. No changes needed. For the future zoom feature, you would need the VLM to output crop coordinates → feed them back → capture.py uses them next turn. That is a separate feature.

### F. Holding base64 data in memory (Persistence class)

**Your proposal**: The Persistence class holds screenshot base64 in memory rather than passing through pipes.

This only helps if you eliminate the subprocess boundaries. Currently:
- `capture.py` runs as subprocess → must output via stdout.
- `execute.py` runs as subprocess → must output via stdout.

If you import `capture` directly into `execute.py` (avoiding one subprocess), you save one pipe transfer of the base64 data. `execute.py` could then be imported into `main.py` (avoiding another subprocess), but then `tools.py`'s `SendInput` calls and GDI capture happen in the same process as the HTTP client, which is fine — they are sequential within a turn.

**Verdict**: This is a valid optimization path, but it is an architectural change (collapsing subprocesses), not just adding a class. If you pursue it, the order of collapse should be:
1. Import `capture` into `execute.py` instead of subprocess call. (Easy, already importable.)
2. Import `execute` into `main.py` instead of subprocess call. (Moderate, requires isolating `tools.py` global state per turn.)
3. At that point, a `Persistence`-like class makes sense as the single state container passed through the pipeline.

Not urgent. The current architecture works and the timing is dominated by VLM inference.

---

## Specific Code Issues

### panel.py

1. **`_sse_broadcast` sends full turn data including `image_data_uri`** in the SSE event (only `feedback_text_full` is stripped). This pushes megabytes per event per client. Strip `image_data_uri` from SSE events.

2. **`_log_batch` is never flushed on clean shutdown if batch size < 15.** The `main()` function does call `_flush_log()` on shutdown, so this is handled. Good.

3. **`_is_paused` iterates all directories** in `LOG_BASE` looking for any `PAUSED` file. If there are many old run directories, this is O(n) filesystem calls per check. Since `/health` polls this and the SSE keepalive is 15s, this is fine. But `_pause_agent` writes `PAUSED` to the latest `run_` directory, not necessarily `_run_dir`. This could cause a mismatch if `_run_dir` is not the latest. Actually, `_pause_agent` sorts by name descending and picks the first `run_` directory — which is the latest by timestamp. And `_is_paused` checks all directories. So if any run has a `PAUSED` file, the system reports paused. This is correct for the use case but means stale `PAUSED` files from old runs could block the current run. `_unpause_agent` cleans all of them, so this is self-healing.

4. **No CORS headers on non-SSE endpoints.** The SSE endpoint sets `Access-Control-Allow-Origin: *` but other endpoints do not. Since the panel HTML is served from the same origin, this is fine.

### main.py

5. **`importlib.reload(_cfg)` each turn** — this hot-reloads `config.py`. This means you can change temperature, model, etc. without restarting. Good. But `reload` does not reset module-level state if config.py had any (it does not currently, just constants). Fine.

6. **`story = raw if raw and raw.strip() else "click(500, 500)"`** — if VLM returns empty or whitespace, the fallback story is a click at center. This means the system never truly stalls — it always has an action. But this fallback story "click(500, 500)" becomes the SST text for the next turn, so the VLM receives just that string. This is a reasonable recovery mechanism.

7. **`fail_streak` logic** — increments on malformed-only (no executed, yes malformed), resets on any executed action, auto-pauses at 8. The gap: if VLM returns text with no actions and no malformed lines, `fails` is neither incremented nor reset. So a VLM that outputs pure narrative with no function calls will never trigger auto-pause and never reset the counter. This is probably fine — the counter only matters when the VLM is actively producing broken calls.

### execute.py

8. **`_extract_calls` parses each line as `ast.parse(line, mode="eval")`** — this means multi-line calls (e.g., a call with arguments spanning lines) are not detected. This is by design (VLM outputs single-line calls). Correct.

9. **`eval(compile(line, "<agent>", "eval"), ns)`** — execution happens with a restricted namespace. The `__builtins__` dict is limited to safe builtins. However, an attacker (or hallucinating VLM) could construct something like `type('',(),{'__del__':lambda s:...})()` using the allowed `type` builtin. Removing `type` from `_SAFE_BUILTINS` would close this. In practice, since the VLM is local and the calls are pre-filtered to known function names, this is low risk.

10. **`ns["print"] = lambda *a, **k: tools.write(...)`** — redirects `print()` to `tools.write()`, which types text physically (if `PHYSICAL_EXECUTION=True`). A VLM that generates `print("hello")` will cause keyboard input. This is intentional but worth being aware of.

### tools.py

11. **`_smooth_move` does 21 steps with 10ms sleep** — 210ms of cursor movement per click. With a click delay of 120ms, each click action takes ~330ms minimum. A drag involves two smooth moves (~420ms) plus delays (~200ms) = ~620ms. These are reasonable for visual feedback.

12. **`_remap` is applied only when `_crop_active`** — correct. When crop is off, coordinates map linearly to full screen.

### capture.py

13. **`encode_png` is a manual PNG encoder** — this avoids any dependency on PIL/Pillow. The implementation is correct (IHDR with color type 6 = RGBA, filter byte 0 = None per row, zlib compress level 6). For 1920×1080 this produces ~3-5MB PNGs. Using filter type 0 (None) for every row is suboptimal for compression but avoids complexity. Acceptable.

14. **DPI awareness is set at module import time** — `ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)` at the top level. This means any process that imports `capture.py` gets DPI awareness set. Since `panel.py` imports `capture.preview_b64`, the panel process also becomes DPI-aware. This is fine and actually desirable.

---

## Recommendations Summary

| # | Item | Priority | Action |
|---|------|----------|--------|
| 1 | Strip `image_data_uri` from SSE events | High | Prevents multi-MB SSE pushes per turn |
| 2 | Auto-refresh preview in region overlay | Low | Add `setInterval`/`clearInterval` in JS, 2-3s polling |
| 3 | Persistence class | Not recommended | File-based IPC is correct for multi-process architecture |
| 4 | Dynamic tool switching | Already works | No change needed |
| 5 | Crop/resize correctness | Already correct | No change needed |
| 6 | Region modification during pause | Already works | No change needed |
| 7 | Remove `type` from `_SAFE_BUILTINS` | Low | Minor hardening of eval sandbox |
| 8 | Collapse `capture.py` subprocess into `execute.py` import | Medium | Saves ~200-400ms and one pipe transfer per turn; `capture` is already importable |
| 9 | Stale `PAUSED` files from old runs | Low | `_is_paused` could check only `_run_dir` instead of all directories |

The architecture is sound for its purpose. The main design tension is between multi-process isolation (good for GDI/DPI safety, clean subprocess boundaries) and efficiency (process startup cost, data copying through pipes). Given that VLM inference dominates latency by 1-2 orders of magnitude, the current tradeoffs are well-chosen. The most impactful change would be item 1 (SSE data size) and item 8 (eliminating one subprocess level).
