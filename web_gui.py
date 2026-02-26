#!/usr/bin/env python3
"""
PaperRouter - Web Interface
A Flask-based GUI served in the browser.
Run:  python web_gui.py
Then open http://localhost:5000 in your browser.
"""

import sys
import os
import re
import json
import signal
import subprocess
import threading
import atexit
import webbrowser
import time
from pathlib import Path
from queue import Queue, Empty

from flask import Flask, Response, request, jsonify

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
DOWNLOADER_SCRIPT = SCRIPT_DIR / "downloader.py"
HARNESS_SCRIPT = SCRIPT_DIR / "harness.py"
LCCN_PATTERN = re.compile(r'^[a-z]{1,3}\d{8,10}$')
METADATA_FILE = "download_metadata.json"
PROGRESS_RE = re.compile(r'\[(\d+)/(\d+)\]\s+Processing')
FOUND_ISSUES_RE = re.compile(r'(?:Found|Will process)\s+(\d+)\s+issues')
EMPTY_WARNING_RE = re.compile(r'No issues found matching criteria')
PAGE_RE = re.compile(r'\[page\s+(\d+)/(\d+)\]\s+done')
ISSUE_PAGES_RE = re.compile(r'Issue has (\d+) pages')

PORT_CANDIDATES = [5000, 5001, 8080, 5005, 8081, 8082, 8083, 8084]
UPDATER_SCRIPT = SCRIPT_DIR / "updater.py"
VERSION_FILE = SCRIPT_DIR / "VERSION"


def _get_version():
    """Read the local VERSION file."""
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    return "unknown"


def _needs_harness(ocr_mode: str) -> bool:
    return ocr_mode in ('surya', 'both')


# ---------------------------------------------------------------------------
# Download process manager (singleton)
# ---------------------------------------------------------------------------
class DownloadManager:
    """Manages a single download subprocess and streams its output."""

    def __init__(self):
        self.process = None
        self.is_running = False
        self._using_harness = False
        self.log_lines = []       # full log history for late-joining clients
        self.subscribers = []     # list of Queue objects for SSE clients
        self.lock = threading.Lock()
        self.progress = {"current": 0, "total": 0, "page_current": 0, "page_total": 0}
        self._stopped = False   # set by stop() so _reader knows to exit quietly

    def start(self, cmd, use_harness=False):
        with self.lock:
            if self.is_running:
                return False, "Download already in progress"
            self.log_lines.clear()
            self.progress = {"current": 0, "total": 0, "page_current": 0, "page_total": 0}
            self.is_running = True
            self._stopped = False
            self._using_harness = use_harness

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creation_flags,
            env=env,
        )

        threading.Thread(target=self._reader, daemon=True).start()
        return True, "Started"

    def stop(self):
        with self.lock:
            if not self.is_running or not self.process:
                return False, "No download in progress"
            self._stopped = True

        self._kill_process()
        self._broadcast("event: log\ndata: " + json.dumps({"line": "\nStopped by user.\n"}) + "\n\n")
        self._finish("stopped")
        return True, "Stopped"

    def subscribe(self):
        q = Queue()
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    # -- internals --

    def _reader(self):
        """Read subprocess stdout line-by-line and broadcast to SSE clients."""
        proc = self.process  # local ref so stop() setting self.process=None is safe
        try:
            for line in iter(proc.stdout.readline, ''):
                self._parse_progress(line)
                event = "event: log\ndata: " + json.dumps({"line": line}) + "\n\n"
                with self.lock:
                    self.log_lines.append(line)
                self._broadcast(event)

            proc.wait()

            # If stop() already handled cleanup, don't send duplicate events
            if self._stopped:
                return

            rc = proc.returncode
            status = "success" if rc == 0 else "error"
            if rc != 0:
                msg = f"\nProcess exited with code {rc}\n"
                self._broadcast("event: log\ndata: " + json.dumps({"line": msg}) + "\n\n")
        except Exception as e:
            if self._stopped:
                return
            status = "error"
            self._broadcast("event: log\ndata: " + json.dumps({"line": f"\nError: {e}\n"}) + "\n\n")

        self._finish(status)

    def _finish(self, status):
        with self.lock:
            self.is_running = False
            self.process = None
        self._broadcast("event: done\ndata: " + json.dumps({"status": status, "progress": self.progress}) + "\n\n")

    def _parse_progress(self, line):
        changed = False
        m = PROGRESS_RE.search(line)
        if m:
            self.progress.update({"current": int(m.group(1)), "total": int(m.group(2)),
                                  "page_current": 0, "page_total": self.progress.get("page_total", 0)})
            changed = True
        else:
            m = ISSUE_PAGES_RE.search(line)
            if m:
                self.progress.update({"page_total": int(m.group(1)), "page_current": 0})
                changed = True
            else:
                m = PAGE_RE.search(line)
                if m:
                    self.progress["page_current"] = self.progress.get("page_current", 0) + 1
                    self.progress["page_total"] = int(m.group(2))
                    changed = True
                else:
                    m = FOUND_ISSUES_RE.search(line)
                    if m:
                        self.progress = {"current": 0, "total": int(m.group(1)),
                                         "page_current": 0, "page_total": 0}
                        changed = True
                    elif EMPTY_WARNING_RE.search(line):
                        self.progress = {"current": 0, "total": 0,
                                         "page_current": 0, "page_total": 0}
                        changed = True

        if changed:
            event = "event: progress\ndata: " + json.dumps(self.progress) + "\n\n"
            self._broadcast(event)

    def _broadcast(self, event_str):
        with self.lock:
            dead = []
            for q in self.subscribers:
                try:
                    q.put_nowait(event_str)
                except Exception:
                    dead.append(q)
            for q in dead:
                if q in self.subscribers:
                    self.subscribers.remove(q)

    def _kill_process(self):
        if not self.process or self.process.poll() is not None:
            return
        if self._using_harness and HARNESS_SCRIPT.exists():
            try:
                subprocess.Popen(
                    [sys.executable, str(HARNESS_SCRIPT), "--kill"],
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
                )
            except Exception:
                pass
        try:
            if sys.platform == 'win32':
                # Force kill the process and all its children. 
                # This ensures harness.py and its children (downloader and workers) are gone.
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(self.process.pid)],
                               creationflags=subprocess.CREATE_NO_WINDOW,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                # Double-check for harness.py's specific PID file if it exists
                if HARNESS_SCRIPT.exists() and (SCRIPT_DIR / ".harness.pid").exists():
                    try:
                        h_pid_str = (SCRIPT_DIR / ".harness.pid").read_text().strip()
                        if h_pid_str.isdigit():
                            h_pid = int(h_pid_str)
                            subprocess.run(['taskkill', '/F', '/T', '/PID', str(h_pid)],
                                           creationflags=subprocess.CREATE_NO_WINDOW,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except:
                        pass
            else:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        except Exception as e:
            print(f"Error killing process: {e}")


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
dm = DownloadManager()


@app.route("/")
def index():
    return HTML_PAGE


@app.route("/api/status")
def status():
    return jsonify({
        "running": dm.is_running,
        "progress": dm.progress,
    })


@app.route("/api/download/start", methods=["POST"])
def download_start():
    data = request.json or {}
    lccn = (data.get("lccn") or "").strip()
    if not lccn:
        return jsonify({"ok": False, "error": "LCCN is required"}), 400

    if not DOWNLOADER_SCRIPT.exists():
        return jsonify({"ok": False, "error": "downloader.py not found"}), 500

    ocr_mode = data.get("ocr", "none")
    use_harness = _needs_harness(ocr_mode)
    launcher = str(HARNESS_SCRIPT) if use_harness else str(DOWNLOADER_SCRIPT)

    cmd = [
        sys.executable, launcher,
        "--source", data.get("source", "loc"),
        "--lccn", lccn,
        "--output", data.get("output", "downloads"),
        "--speed", data.get("speed", "safe"),
    ]

    years = (data.get("years") or "").strip()
    if years:
        cmd.extend(["--years", years])
    if data.get("max_issues"):
        cmd.extend(["--max-issues", str(data.get("max_issues"))])
    if data.get("verbose"):
        cmd.append("--verbose")
    if data.get("retry_failed"):
        cmd.append("--retry-failed")
    if ocr_mode != "none":
        cmd.extend(["--ocr", ocr_mode])
    if data.get("ocr_batch"):
        cmd.append("--ocr-batch")
    if data.get("force_ocr"):
        cmd.append("--force-ocr")
    ocr_date = (data.get("ocr_date") or "").strip()
    if ocr_date:
        cmd.extend(["--date", ocr_date])

    ok, msg = dm.start(cmd, use_harness=use_harness)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 409


@app.route("/api/download/stop", methods=["POST"])
def download_stop():
    ok, msg = dm.stop()
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 409


@app.route("/api/download/stream")
def download_stream():
    """SSE endpoint — streams log lines, progress updates, and done events."""
    q = dm.subscribe()

    def generate():
        # Send current state so the client knows what's happening
        yield "event: status\ndata: " + json.dumps({
            "running": dm.is_running,
            "progress": dm.progress,
        }) + "\n\n"
        # Replay existing log lines for late-joining clients
        with dm.lock:
            for line in dm.log_lines:
                yield "event: log\ndata: " + json.dumps({"line": line}) + "\n\n"
        # Stream new events
        while True:
            try:
                event = q.get(timeout=30)
                yield event
            except Empty:
                # Send keep-alive comment to prevent timeout
                yield ": keepalive\n\n"

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"

    @resp.call_on_close
    def cleanup():
        dm.unsubscribe(q)

    return resp


@app.route("/api/search", methods=["POST"])
def search():
    data = request.json or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify([])
    try:
        cmd = [
            sys.executable, str(DOWNLOADER_SCRIPT),
            "--source", data.get("source", "loc"),
            "--search", query, "--json",
        ]
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                creationflags=creation_flags)
        return Response(result.stdout, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/lookup", methods=["POST"])
def lookup():
    data = request.json or {}
    lccn = (data.get("lccn") or "").strip()
    if not lccn:
        return jsonify({})
    try:
        cmd = [
            sys.executable, str(DOWNLOADER_SCRIPT),
            "--source", data.get("source", "loc"),
            "--info", lccn, "--json",
        ]
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                creationflags=creation_flags)
        return Response(result.stdout, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/browse", methods=["POST"])
def browse():
    """Open a native directory picker and return the selected path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return jsonify({"path": "", "error": "Manual path entry required: Tkinter not installed"}), 200
    root = tk.Tk()
    root.withdraw()
    # Bring to front
    root.attributes('-topmost', True)
    folder_path = filedialog.askdirectory()
    root.destroy()
    return jsonify({"path": folder_path})


@app.route("/api/metadata", methods=["GET"])
def metadata():
    """Read download_metadata.json and return a year-by-year summary with OCR coverage."""
    output_dir = request.args.get("output", "downloads")
    scan_ocr = request.args.get("scan_ocr", "false").lower() == "true"
    output_path = Path(output_dir)
    meta_path = output_path / METADATA_FILE
    if not meta_path.exists():
        return jsonify({"found": False})
    try:
        with open(meta_path, 'r') as f:
            meta = json.load(f)
    except (json.JSONDecodeError, IOError):
        return jsonify({"found": False, "error": "Could not read metadata"})

    downloaded = meta.get('downloaded', {})
    years = {}
    total_issues = 0
    total_pages = 0
    for issue_id, info in downloaded.items():
        year_str = info['date'][:4]
        pages = info.get('pages', [])
        page_count = len(pages)
        if year_str not in years:
            years[year_str] = {"issues": 0, "pages": 0, "loc_ocr": 0, "surya_ocr": 0}
        years[year_str]["issues"] += 1
        years[year_str]["pages"] += page_count
        total_issues += 1
        total_pages += page_count

        if scan_ocr:
            year_dir = output_path / year_str
            for page_info in pages:
                base = f"{info['date']}_ed-{info['edition']}_page{page_info['page']:02d}"
                if (year_dir / f"{base}_loc.txt").exists():
                    years[year_str]["loc_ocr"] += 1
                if (year_dir / f"{base}_surya.txt").exists():
                    years[year_str]["surya_ocr"] += 1

    return jsonify({
        "found": True,
        "lccn": meta.get('lccn', ''),
        "title": meta.get('newspaper_title', ''),
        "years": dict(sorted(years.items())),
        "total_issues": total_issues,
        "total_pages": total_pages
    })


@app.route("/api/version")
def version():
    return jsonify({"version": _get_version()})


@app.route("/api/update/check", methods=["GET"])
def update_check():
    """Check for updates via the updater module."""
    if not UPDATER_SCRIPT.exists():
        return jsonify({"update_available": False, "error": "updater.py not found"})
    try:
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        result = subprocess.run(
            [sys.executable, str(UPDATER_SCRIPT), "--check-only", "--json"],
            capture_output=True, text=True, timeout=10,
            creationflags=creation_flags,
        )
        data = json.loads(result.stdout.strip())
        return jsonify(data)
    except Exception as e:
        return jsonify({"update_available": False, "error": str(e)})


@app.route("/api/update/apply", methods=["POST"])
def update_apply():
    """Download and apply an update."""
    if not UPDATER_SCRIPT.exists():
        return jsonify({"ok": False, "error": "updater.py not found"}), 500
    try:
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        result = subprocess.run(
            [sys.executable, str(UPDATER_SCRIPT), "--apply", "--json"],
            capture_output=True, text=True, timeout=120,
            creationflags=creation_flags,
        )
        data = json.loads(result.stdout.strip())
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def _cleanup():
    dm._kill_process()

atexit.register(_cleanup)


# ---------------------------------------------------------------------------
# HTML page (embedded)
# ---------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PaperRouter — Historical Newspaper Suite</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Outfit:wght@500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0f172a;
    --card: rgba(30, 41, 59, 0.7);
    --card-border: rgba(255, 255, 255, 0.1);
    --primary: #3b82f6;
    --primary-hover: #2563eb;
    --accent: #8b5cf6;
    --danger: #ef4444;
    --danger-hover: #dc2626;
    --text: #f8fafc;
    --muted: #94a3b8;
    --radius-lg: 12px;
    --radius-md: 8px;
    --shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Inter', -apple-system, sans-serif;
    background: var(--bg);
    background-image: radial-gradient(circle at top right, rgba(59, 130, 246, 0.1), transparent),
                      radial-gradient(circle at bottom left, rgba(139, 92, 246, 0.1), transparent);
    color: var(--text);
    line-height: 1.6;
    max-width: 900px; margin: 0 auto; padding: 40px 20px;
  }
  header { margin-bottom: 32px; text-align: left; }
  h1 { 
    font-family: 'Outfit', sans-serif;
    font-size: 2.25rem; font-weight: 700; 
    background: linear-gradient(to right, #fff, #94a3b8);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    margin-bottom: 4px;
  }
  .subtitle { color: var(--muted); font-size: 1rem; }
  
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
  @media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }

  .card {
    background: var(--card);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--card-border);
    border-radius: var(--radius-lg);
    padding: 24px;
    box-shadow: var(--shadow);
    transition: transform 0.2s, border-color 0.2s;
  }
  /* .card:hover { border-color: rgba(59, 130, 246, 0.3); } */

  .card h2 { 
    font-family: 'Outfit', sans-serif;
    font-size: 0.8rem; color: var(--primary); text-transform: uppercase;
    letter-spacing: 0.1em; margin-bottom: 16px; font-weight: 700;
  }

  label { display: block; font-size: 0.8rem; font-weight: 600; color: var(--muted); margin-bottom: 6px; }
  
  input[type="text"], input[type="number"], select {
    width: 100%; padding: 10px 14px; 
    background: rgba(15, 23, 42, 0.6);
    border: 1px solid var(--card-border);
    border-radius: var(--radius-md); 
    font-size: 0.9rem; color: #fff;
    transition: all 0.2s;
  }
  input[type="text"]:focus, input[type="number"]:focus, select:focus {
    outline: none; border-color: var(--primary); 
    background: rgba(15, 23, 42, 0.8);
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2);
  }

  .field-row { display: flex; gap: 10px; margin-bottom: 16px; align-items: flex-end; }
  .field-row > div { flex: 1; }
  .field-row.compact { margin-bottom: 0; }

  button {
    padding: 10px 20px; border: none; border-radius: var(--radius-md);
    font-size: 0.9rem; font-weight: 600; cursor: pointer; 
    transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover { background: var(--primary-hover); transform: translateY(-1px); }
  .btn-primary:active { transform: translateY(0); }
  .btn-primary:disabled { background: #1e293b; color: var(--muted); cursor: not-allowed; transform: none; }

  .btn-danger { background: rgba(239, 68, 68, 0.1); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.2); }
  .btn-danger:hover { background: var(--danger); color: #fff; }
  .btn-danger:disabled { opacity: 0.3; cursor: not-allowed; }

  .btn-secondary { background: rgba(255, 255, 255, 0.05); color: #fff; border: 1px solid var(--card-border); }
  .btn-secondary:hover { background: rgba(255, 255, 255, 0.1); }

  /* Newspaper Listing */
  #search-results {
    margin-top: 12px; max-height: 200px; overflow-y: auto;
    border-radius: var(--radius-md); background: rgba(15, 23, 42, 0.4);
    display: none; border: 1px solid var(--card-border);
  }
  .result-item {
    padding: 12px 16px; cursor: pointer; border-bottom: 1px solid var(--card-border);
    transition: background 0.15s; display: flex; flex-direction: column;
  }
  .result-item:hover { background: rgba(59, 130, 246, 0.1); }
  .result-item .r-title { font-weight: 600; font-size: 0.95rem; }
  .result-item .r-meta { font-size: 0.8rem; color: var(--muted); margin-top: 2px; }

  /* Preview Card */
  .preview-box {
    display: flex; gap: 16px; align-items: center; margin-top: 12px;
    padding: 12px; background: rgba(255,255,255,0.03); border-radius: var(--radius-md);
    border: 1px solid var(--card-border);
  }
  .preview-thumb {
    width: 60px; height: 80px; background: #1e293b; border-radius: 4px;
    object-fit: cover; border: 1px solid var(--card-border);
  }
  .preview-info { flex: 1; }
  .preview-info h3 { font-size: 1rem; font-weight: 600; margin-bottom: 2px; }
  .preview-info p { font-size: 0.8rem; color: var(--muted); }

  /* Toggle Group */
  .option-group { margin-bottom: 20px; }
  .toggle-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
  .chip {
    padding: 6px 12px; border-radius: 20px; font-size: 0.8rem; font-weight: 500;
    cursor: pointer; background: rgba(255,255,255,0.05); border: 1px solid var(--card-border);
    transition: all 0.2s; color: var(--muted);
  }
  .chip:hover { background: rgba(255,255,255,0.08); color: #fff; }
  .chip.active { background: var(--primary); color: #fff; border-color: var(--primary); }

  /* Progress Section */
  .progress-section { margin-top: 32px; }
  .progress-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .progress-stats { font-size: 0.9rem; font-weight: 600; }
  .progress-status { font-size: 0.8rem; color: var(--muted); }

  .progress-container {
    height: 10px; background: rgba(255,255,255,0.05); border-radius: 10px;
    overflow: hidden; position: relative;
  }
  .progress-bar {
    height: 100%; background: linear-gradient(90deg, var(--primary), var(--accent));
    width: 0%; transition: width 0.4s cubic-bezier(0.1, 0.7, 0.1, 1);
    box-shadow: 0 0 10px rgba(59, 130, 246, 0.5);
  }
  .progress-bar.indeterminate {
    width: 40% !important;
    animation: slide 1.5s infinite linear;
  }
  @keyframes slide {
    from { transform: translateX(-100%); }
    to { transform: translateX(250%); }
  }

  /* Log Viewer */
  .log-card { margin-top: 24px; padding: 0; overflow: hidden; }
  .log-header { 
    display: flex; justify-content: space-between; align-items: center; 
    padding: 12px 20px; background: rgba(0,0,0,0.2); border-bottom: 1px solid var(--card-border);
  }
  #log {
    height: 350px; overflow-y: auto; padding: 20px;
    font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 0.85rem; color: #cbd5e1; line-height: 1.6;
    background: rgba(15, 23, 42, 0.8);
    scrollbar-width: thin; scrollbar-color: var(--card-border) transparent;
    white-space: pre-wrap; word-wrap: break-word;
  }
  #log::-webkit-scrollbar { width: 6px; }
  #log::-webkit-scrollbar-thumb { background: var(--card-border); border-radius: 3px; }
  #log.collapsed { display: none; }

  /* Collapsible sections */
  .collapsible-header {
    display: flex; justify-content: space-between; align-items: center;
    cursor: pointer; user-select: none; padding: 16px 24px;
  }
  .collapsible-header:hover { background: rgba(255,255,255,0.02); }
  .collapsible-body { padding: 0 24px 24px; }
  .collapsible-body.collapsed { display: none; }
  .collapsible-badge {
    font-size: 0.75rem; color: var(--muted); background: rgba(255,255,255,0.05);
    padding: 2px 10px; border-radius: 12px; margin-left: 12px;
  }
  .chevron { transition: transform 0.2s; display: inline-block; font-size: 0.8rem; color: var(--muted); }
  .chevron.open { transform: rotate(180deg); }

  /* Modals */
  .overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.8); backdrop-filter: blur(4px);
    display: none; align-items: center; justify-content: center; z-index: 100;
  }
  .modal {
    background: var(--bg); border: 1px solid var(--card-border); border-radius: var(--radius-lg);
    padding: 32px; width: 90%; max-width: 400px; text-align: center;
    box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5);
  }
  .spinner {
    width: 48px; height: 48px; border: 4px solid rgba(59, 130, 246, 0.1);
    border-top-color: var(--primary); border-radius: 50%;
    animation: spin 0.8s linear infinite; margin: 0 auto 20px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Utils */
  .checkbox-label {
    display: inline-flex; align-items: center; gap: 8px; font-size: 0.85rem;
    cursor: pointer; color: var(--muted); transition: color 0.2s;
  }
  .checkbox-label:hover { color: #fff; }
  input[type="checkbox"] { accent-color: var(--primary); width: 16px; height: 16px; }
</style>
</head>
<body>

<div id="overlay" class="overlay">
  <div class="modal">
    <div class="spinner"></div>
    <h3 id="modal-title" style="margin-bottom:8px">Processing</h3>
    <p id="modal-msg" style="color:var(--muted); font-size:0.9rem">Please wait while we fetch the latest data...</p>
  </div>
</div>

<header>
  <div style="display:flex; justify-content:space-between; align-items:flex-start">
    <div>
      <h1>PaperRouter</h1>
      <p class="subtitle">Historical Newspaper Archive & OCR Suite <span id="version-label" style="opacity:0.5"></span></p>
    </div>
  </div>
</header>

<div id="update-banner" style="display:none; margin-bottom:20px; padding:14px 20px; background:rgba(139,92,246,0.1); border:1px solid rgba(139,92,246,0.3); border-radius:var(--radius-md); font-size:0.85rem">
  <div style="display:flex; justify-content:space-between; align-items:center">
    <span id="update-text"></span>
    <div style="display:flex; gap:10px; align-items:center">
      <button class="btn-primary" onclick="applyUpdate()" id="btn-update" style="padding:6px 16px; font-size:0.8rem">Update Now</button>
      <button style="background:none; border:none; color:var(--muted); cursor:pointer; font-size:1.1rem; padding:4px" onclick="this.parentElement.parentElement.parentElement.style.display='none'" title="Dismiss">&times;</button>
    </div>
  </div>
</div>

<div class="grid">
  <!-- Search & Identity -->
  <div class="card">
    <h2>Identity</h2>
    <div class="field-row">
      <div>
        <label for="lccn">LCCN (Control Number)</label>
        <input type="text" id="lccn" value="sn87080287" placeholder="sn87080287">
      </div>
      <button class="btn-secondary" onclick="lookupLCCN()" style="height:41px">Lookup</button>
    </div>
    
    <div class="field-row">
      <div>
        <label for="search-input">Search Title</label>
        <input type="text" id="search-input" placeholder="e.g. New York Tribune" 
               onkeydown="if(event.key==='Enter')searchNewspapers()">
      </div>
      <button class="btn-secondary" onclick="searchNewspapers()" style="height:41px">Search</button>
    </div>

    <div id="search-results"></div>

    <div id="selection-preview" style="display:none" class="preview-box">
      <!-- Thumbnail & Info injected here -->
    </div>
  </div>

  <!-- Configuration -->
  <div class="card">
    <h2>Configuration</h2>
    <div class="field-row">
      <div>
        <label for="output">Output Directory</label>
        <input type="text" id="output" value="downloads">
      </div>
      <button class="btn-secondary" onclick="browseFolder()" style="height:41px">Browse</button>
    </div>

    <div class="field-row" style="margin-bottom: 20px;">
      <div>
        <label for="years">Years (e.g. 1900-1905)</label>
        <input type="text" id="years" placeholder="All available">
      </div>
      <div>
        <label for="max-issues">Max Issues</label>
        <input type="number" id="max-issues" placeholder="Unlimited" min="1">
      </div>
    </div>

    <div class="option-group">
      <label>OCR Engine</label>
      <div class="toggle-row" id="ocr-group">
        <div class="chip active" data-value="none">None</div>
        <div class="chip" data-value="loc">LOC (Fast)</div>
        <div class="chip" data-value="surya">Surya (AI)</div>
        <div class="chip" data-value="both">Both</div>
      </div>
    </div>

    <div style="margin-top:4px">
      <button class="btn-secondary" onclick="toggleAdvanced()" id="btn-advanced"
              style="font-size:0.75rem; padding:4px 12px; width:100%; text-align:center">
        Show Advanced Options &#9660;
      </button>
    </div>

    <div id="advanced-options" style="display:none; margin-top:16px">
      <div class="option-group">
        <label>Speed Profile</label>
        <div class="toggle-row" id="speed-group">
          <div class="chip active" data-value="safe">Safe (15s)</div>
          <div class="chip" data-value="standard">Standard (4s)</div>
        </div>
      </div>

      <div class="field-row compact" style="gap:20px; margin-top:10px">
        <label class="checkbox-label"><input type="checkbox" id="verbose"> Verbose Log</label>
        <label class="checkbox-label"><input type="checkbox" id="retry-failed"> Retry Failed</label>
      </div>
    </div>
  </div>
</div>

<!-- Controls -->
<div class="card" style="margin-bottom:32px">
  <div style="display:flex; gap:12px; align-items:center">
    <button id="btn-start" class="btn-primary" onclick="startDownload()" style="min-width:140px">
      Start Download
    </button>
    <button id="btn-stop" class="btn-danger" onclick="stopDownload()" disabled>Stop</button>
  </div>

  <div class="progress-section">
    <div class="progress-header">
      <div class="progress-stats" id="progress-stats">Ready to start</div>
      <div class="progress-status" id="status-bar">Connection idle</div>
    </div>
    <div class="progress-container">
      <div id="progress-fill" class="progress-bar"></div>
    </div>
  </div>
</div>

<!-- Downloads Summary -->
<div class="card" id="downloads-panel" style="margin-bottom:32px; display:none; padding:0; overflow:hidden">
  <div class="collapsible-header" onclick="toggleSection('downloads')">
    <div style="display:flex; align-items:center">
      <h2 style="margin:0; font-size:1rem">Downloaded Collection</h2>
      <span id="downloads-badge" class="collapsible-badge" style="display:none"></span>
    </div>
    <div style="display:flex; align-items:center; gap:8px">
      <button class="btn-secondary" onclick="event.stopPropagation(); scanDownloads()" style="font-size:0.75rem; padding:4px 10px">Refresh</button>
      <span class="chevron" id="downloads-chevron">&#9660;</span>
    </div>
  </div>
  <div class="collapsible-body collapsed" id="downloads-body">
    <div id="downloads-summary" style="font-size:0.85rem; color:var(--muted)">No data yet.</div>
  </div>
</div>

<!-- OCR Manager -->
<div class="card" id="ocr-manager-panel" style="margin-bottom:32px; display:none; padding:0; overflow:hidden">
  <div class="collapsible-header" onclick="toggleSection('ocr')">
    <div style="display:flex; align-items:center">
      <h2 style="margin:0; font-size:1rem">OCR Manager</h2>
      <span id="ocr-badge" class="collapsible-badge" style="display:none"></span>
    </div>
    <div style="display:flex; align-items:center; gap:8px">
      <button class="btn-secondary" onclick="event.stopPropagation(); loadOCRManager()" id="btn-ocr-scan" style="font-size:0.75rem; padding:4px 10px">Scan OCR Coverage</button>
      <span class="chevron" id="ocr-chevron">&#9660;</span>
    </div>
  </div>
  <div class="collapsible-body collapsed" id="ocr-body">
    <div style="font-size:0.72rem; color:var(--muted); margin-bottom:12px">Select which years to OCR — only pages missing the chosen engine will be processed</div>

    <div id="ocr-manager-placeholder" style="font-size:0.85rem; color:var(--muted); padding:8px 0">Click "Scan OCR Coverage" to see what's been downloaded and which pages have text.</div>

    <div id="ocr-manager-body" style="display:none">
      <!-- Year table -->
      <div style="margin-bottom:12px; display:flex; gap:8px">
        <button class="btn-secondary" onclick="ocrSelectAll(true)" style="font-size:0.75rem; padding:3px 8px">Select All</button>
        <button class="btn-secondary" onclick="ocrSelectAll(false)" style="font-size:0.75rem; padding:3px 8px">Select None</button>
        <button class="btn-secondary" onclick="ocrSelectMissing()" style="font-size:0.75rem; padding:3px 8px">Select Missing</button>
      </div>
      <div id="ocr-year-list" style="display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:8px; margin-bottom:16px"></div>

      <!-- Specific date -->
      <div class="field-row compact" style="margin-bottom:12px">
        <label style="white-space:nowrap; font-size:0.85rem">Specific date</label>
        <input type="text" id="ocr-date" class="input" placeholder="YYYY-MM-DD (optional — targets one issue)" style="max-width:280px">
      </div>

      <!-- OCR engine selection (read from main chips) -->
      <div id="ocr-estimate" style="font-size:0.82rem; color:var(--muted); margin-bottom:14px"></div>

      <div style="display:flex; gap:12px; align-items:center">
        <button class="btn-primary" onclick="runOCRManager()" id="btn-ocr-run">▶ Run OCR on Selected</button>
        <label style="font-size:0.8rem; color:var(--muted); display:flex; align-items:center; gap:4px; cursor:pointer">
          <input type="checkbox" id="ocr-mgr-force"> Force re-run
        </label>
      </div>
    </div>

    <div style="margin-top:16px; padding-top:16px; border-top:1px solid var(--card-border); display:flex; gap:12px; align-items:center">
      <button id="btn-ocr-batch" class="btn-secondary" onclick="startOCRBatch()" disabled title="Select an OCR engine first">Quick OCR (All Years)</button>
      <label style="font-size:0.8rem; color:var(--muted); display:flex; align-items:center; gap:4px; cursor:pointer" title="Overwrite existing OCR text files">
        <input type="checkbox" id="force-ocr"> Force re-run
      </label>
    </div>
  </div>
</div>

<div class="card log-card">
  <div class="log-header">
    <div>
      <h2 style="margin:0">Process Console</h2>
      <div style="font-size:0.75rem; color:var(--muted)">Streamed from downloader.py</div>
    </div>
    <div style="display:flex; align-items:center; gap:10px">
      <span id="log-preview" style="font-size:0.72rem; color:var(--muted); max-width:350px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap"></span>
      <button class="btn-secondary" onclick="clearLog()" style="font-size:0.72rem; padding:3px 10px">Clear</button>
      <button class="btn-secondary" onclick="toggleConsole()" id="btn-console-toggle" style="font-size:0.72rem; padding:3px 10px">&#9660;</button>
    </div>
  </div>
  <div id="log" class="collapsed"></div>
</div>

<script>
const $ = id => document.getElementById(id);
const val = id => $(id).value.trim();

// Chip Logic
document.querySelectorAll('.toggle-row .chip').forEach(chip => {
  chip.onclick = function() {
    this.parentElement.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    this.classList.add('active');
    // Fix 3: Enable/disable Retroactive OCR based on OCR chip
    if (this.parentElement.id === 'ocr-group') {
      $('btn-ocr-batch').disabled = (this.dataset.value === 'none');
    }
  };
});

function getActiveChip(groupId) {
  return $(groupId).querySelector('.chip.active').dataset.value;
}

function showOverlay(title, msg) {
  $('modal-title').textContent = title;
  $('modal-msg').textContent = msg;
  $('overlay').style.display = 'flex';
}

function hideOverlay() {
  $('overlay').style.display = 'none';
}

let eventSource = null;

function setRunning(running) {
  $('btn-start').disabled = running;
  $('btn-stop').disabled = !running;
  // Only re-enable Retroactive OCR if OCR chip is not 'none'
  const ocrChipValue = getActiveChip('ocr-group');
  $('btn-ocr-batch').disabled = running || ocrChipValue === 'none';
  $('status-bar').textContent = running ? 'Process active' : 'Process idle';
  if (running) {
    $('progress-fill').classList.add('indeterminate');
    $('progress-stats').textContent = 'Initialising...';
  } else {
    $('progress-fill').classList.remove('indeterminate');
  }
}

function updateProgress(d) {
  const cur = d.current || 0, tot = d.total || 0;
  const pgCur = d.page_current || 0, pgTot = d.page_total || 0;
  let pct = 0;
  if (tot > 0) {
    const base = (cur - 1) / tot;
    const pageFrac = pgTot > 0 ? (pgCur / pgTot) / tot : 0;
    pct = Math.round(Math.max(0, Math.min(100, (base + pageFrac) * 100)));
  }
  $('progress-fill').classList.remove('indeterminate');
  $('progress-fill').style.width = pct + '%';
  if (tot > 0) {
    let text = `Issue ${cur} of ${tot}`;
    if (pgTot > 0) text += ` \u2014 Page ${pgCur} of ${pgTot}`;
    $('progress-stats').textContent = text;
  } else {
    $('progress-stats').textContent = 'No issues found';
  }
}

function appendLog(text) {
  const log = $('log');
  const needsScroll = log.scrollTop + log.offsetHeight >= log.scrollHeight - 20;
  log.textContent += text;
  if (needsScroll && consoleExpanded) log.scrollTop = log.scrollHeight;
  const trimmed = text.trim();
  if (trimmed) {
    const lastLine = trimmed.split('\n').pop();
    $('log-preview').textContent = lastLine;
  }
}

let consoleExpanded = false;

function toggleConsole() {
  consoleExpanded = !consoleExpanded;
  const log = $('log');
  const btn = $('btn-console-toggle');
  if (consoleExpanded) {
    log.classList.remove('collapsed');
    btn.innerHTML = '&#9650;';
    log.scrollTop = log.scrollHeight;
  } else {
    log.classList.add('collapsed');
    btn.innerHTML = '&#9660;';
  }
}

function clearLog() {
  $('log').textContent = '';
  $('log-preview').textContent = '';
}

function toggleAdvanced() {
  const el = $('advanced-options');
  const btn = $('btn-advanced');
  if (el.style.display === 'none') {
    el.style.display = 'block';
    btn.innerHTML = 'Hide Advanced Options &#9650;';
  } else {
    el.style.display = 'none';
    btn.innerHTML = 'Show Advanced Options &#9660;';
  }
}

const sectionState = { downloads: false, ocr: false };

function toggleSection(name) {
  sectionState[name] = !sectionState[name];
  const bodyId = name === 'downloads' ? 'downloads-body' : 'ocr-body';
  const chevronId = name === 'downloads' ? 'downloads-chevron' : 'ocr-chevron';
  const body = $(bodyId);
  const chevron = $(chevronId);
  if (sectionState[name]) {
    body.classList.remove('collapsed');
    chevron.classList.add('open');
  } else {
    body.classList.add('collapsed');
    chevron.classList.remove('open');
  }
}

function connectSSE() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource('/api/download/stream');

  eventSource.addEventListener('log', e => {
    const data = JSON.parse(e.data);
    appendLog(data.line);
  });

  eventSource.addEventListener('progress', e => {
    const data = JSON.parse(e.data);
    updateProgress(data);
  });

  eventSource.addEventListener('status', e => {
    const data = JSON.parse(e.data);
    setRunning(data.running);
  });

  eventSource.addEventListener('done', e => {
    const data = JSON.parse(e.data);
    setRunning(false);
    if (data.status === 'success') {
      updateProgress({current: data.progress.total, total: data.progress.total, page_current: 0, page_total: 0});
      $('progress-stats').textContent = data.progress.total > 0 ? 'Download complete' : 'Discovery finished';
    } else {
      $('progress-stats').textContent = data.status === 'stopped' ? 'Process stopped' : 'Process error';
    }
    scanDownloads(); // Refresh collection summary
  });

  eventSource.onerror = () => {
    setTimeout(() => { if (eventSource.readyState === EventSource.CLOSED) connectSSE(); }, 2000);
  };
}

async function startDownload() {
  const lccn = val('lccn');
  if (!lccn) { alert('Enter LCCN first'); return; }
  // If output is still bare "downloads", append LCCN as subfolder (matches CLI default)
  let output = val('output');
  if (output === 'downloads') { output = `downloads/${lccn}`; $('output').value = output; }
  // Remember this LCCN's output directory for next session
  localStorage.setItem(`output_${lccn}`, output);
  clearLog();
  setRunning(true);
  const resp = await fetch('/api/download/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      lccn,
      source: 'loc',
      output,
      speed: getActiveChip('speed-group'),
      ocr: getActiveChip('ocr-group'),
      years: val('years'),
      max_issues: val('max-issues') ? parseInt(val('max-issues'), 10) : null,
      verbose: $('verbose').checked,
      retry_failed: $('retry-failed').checked
    })
  });
  const res = await resp.json();
  if (!res.ok) { setRunning(false); alert(res.error || res.message); }
}

async function stopDownload() { await fetch('/api/download/stop', {method: 'POST'}); }

async function startOCRBatch() {
  const lccn = val('lccn');
  if (!lccn) { alert('Enter LCCN first'); return; }
  const ocrMode = getActiveChip('ocr-group');
  if (ocrMode === 'none') { alert('Select an OCR engine first'); return; }
  let output = val('output');
  if (output === 'downloads') { output = `downloads/${lccn}`; $('output').value = output; }
  localStorage.setItem(`output_${lccn}`, output);
  clearLog();
  setRunning(true);
  const resp = await fetch('/api/download/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      lccn,
      source: 'loc',
      output,
      ocr: ocrMode,
      ocr_batch: true,
      years: val('years'),
      force_ocr: $('force-ocr').checked
    })
  });
  const res = await resp.json();
  if (!res.ok) { setRunning(false); alert(res.error || res.message); }
}

async function searchNewspapers() {
  const query = val('search-input');
  if (!query) return;
  const resultsDiv = $('search-results');
  resultsDiv.innerHTML = '<div style="padding:16px; color:var(--muted)">Searching collection...</div>';
  resultsDiv.style.display = 'block';
  showOverlay('Searching Archive', `Consulting the Library of Congress for "${query}"...`);

  try {
    const resp = await fetch('/api/search', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query, source: 'loc'}),
    });
    const results = await resp.json();
    if (results.length > 0) {
      resultsDiv.innerHTML = results.map(r => `
        <div class="result-item" onclick="selectResult('${r.lccn}')">
          <div style="font-weight:600; font-size:0.95rem">${r.title}</div>
          <div style="color:var(--muted); font-size:0.8rem">${r.lccn} &bull; ${r.place} &bull; ${r.dates}</div>
        </div>
      `).join('');
    } else {
      resultsDiv.innerHTML = '<div style="padding:16px; color:var(--muted)">No results found.</div>';
    }
  } catch (e) {
    resultsDiv.innerHTML = '<div style="padding:16px; color:var(--danger)">Search failed.</div>';
  } finally { hideOverlay(); }
}

function selectResult(lccn) {
  $('lccn').value = lccn;
  $('search-results').style.display = 'none';
  lookupLCCN(); // Auto-lookup on selection
}

async function lookupLCCN() {
  const lccn = val('lccn');
  if (!lccn) return;
  
  const preview = $('selection-preview');
  preview.innerHTML = '<div style="padding:10px; color:var(--muted)">Loading metadata...</div>';
  preview.style.display = 'flex';
  showOverlay('Resolving LCCN', `Fetching details for ${lccn}...`);

  try {
    const resp = await fetch('/api/lookup', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lccn, source: 'loc'}),
    });
    const info = await resp.json();
    if (info && info.title) {
      let thumbHtml = info.thumbnail ? `<img src="${info.thumbnail}" class="preview-thumb">` : `<div class="preview-thumb"></div>`;
      preview.innerHTML = `
        ${thumbHtml}
        <div class="preview-info">
          <h3>${info.title}</h3>
          <p>${info.lccn} &bull; ${info.start_year || '?'}-${info.end_year || '?'}</p>
        </div>
      `;
      const safeTitle = info.title.replace(/[<>:"/\\|?*]/g, '').trim();
      // Check if user previously chose a directory for this LCCN
      const savedOutput = localStorage.getItem(`output_${lccn}`);
      if (savedOutput) {
        $('output').value = savedOutput;
      } else if ($('output').value === 'downloads' || $('output').value === `downloads/${lccn}`) {
        $('output').value = `downloads/${safeTitle}`;
      }
    } else {
      preview.innerHTML = '<div style="padding:10px; color:var(--danger)">Newspaper not found.</div>';
    }
  } catch (e) {
    preview.innerHTML = '<div style="padding:10px; color:var(--danger)">Lookup failed.</div>';
  } finally { hideOverlay(); }
}

async function browseFolder() {
  const resp = await fetch('/api/browse', {method: 'POST'});
  const data = await resp.json();
  if (data.error) {
    alert(data.error);
  } else if (data.path) {
    $('output').value = data.path;
  }
}

function toggleYears() {
  $('years').disabled = radio('year-mode') !== 'custom';
}

// ── OCR Manager ──────────────────────────────────────────────────────────────
let _ocrManagerData = null;

async function loadOCRManager() {
  const output = val('output');
  if (!output) { alert('Set the output directory first'); return; }
  $('btn-ocr-scan').textContent = 'Scanning...';
  $('btn-ocr-scan').disabled = true;
  try {
    const resp = await fetch(`/api/metadata?output=${encodeURIComponent(output)}&scan_ocr=true`);
    const data = await resp.json();
    if (!data.found || !Object.keys(data.years).length) {
      $('ocr-manager-placeholder').textContent = 'No downloaded content found in this output directory.';
      return;
    }
    _ocrManagerData = data;
    $('ocr-manager-placeholder').style.display = 'none';
    const body = $('ocr-manager-body');
    body.style.display = 'block';
    const list = $('ocr-year-list');
    list.innerHTML = '';
    for (const [year, info] of Object.entries(data.years)) {
      const locPct = info.pages > 0 ? Math.round((info.loc_ocr / info.pages) * 100) : 0;
      const suryaPct = info.pages > 0 ? Math.round((info.surya_ocr / info.pages) * 100) : 0;
      list.innerHTML += `
        <label style="display:flex; align-items:center; gap:10px; padding:7px 10px; border:1px solid var(--border); border-radius:8px; cursor:pointer; background:var(--bg)">
          <input type="checkbox" class="ocr-year-cb" data-year="${year}" data-pages="${info.pages}" data-loc="${info.loc_ocr}" data-surya="${info.surya_ocr}" onchange="updateOCREstimate()">
          <span style="font-weight:600; min-width:40px">${year}</span>
          <span style="font-size:0.75rem; color:var(--muted)">${info.issues}i · ${info.pages}p</span>
          <span style="margin-left:auto; font-size:0.72rem">
            <span style="color:${info.loc_ocr >= info.pages ? '#4caf50':'var(--muted)'}" title="LOC OCR">LOC ${locPct}%</span>
            &nbsp;
            <span style="color:${info.surya_ocr >= info.pages ? '#4caf50':'var(--muted)'}" title="Surya OCR">Surya ${suryaPct}%</span>
          </span>
        </label>`;
    }
    updateOCREstimate();
  } catch(e) {
    $('ocr-manager-placeholder').textContent = 'Error scanning: ' + e.message;
  } finally {
    $('btn-ocr-scan').textContent = 'Scan OCR Coverage';
    $('btn-ocr-scan').disabled = false;
  }
}

function ocrSelectAll(checked) {
  document.querySelectorAll('.ocr-year-cb').forEach(cb => cb.checked = checked);
  updateOCREstimate();
}

function ocrSelectMissing() {
  const engine = getActiveChip('ocr-group');
  document.querySelectorAll('.ocr-year-cb').forEach(cb => {
    const pages = parseInt(cb.dataset.pages);
    const loc = parseInt(cb.dataset.loc);
    const surya = parseInt(cb.dataset.surya);
    if (engine === 'loc') cb.checked = (loc < pages);
    else if (engine === 'surya') cb.checked = (surya < pages);
    else if (engine === 'both') cb.checked = (loc < pages || surya < pages);
    else cb.checked = true;
  });
  updateOCREstimate();
}

function updateOCREstimate() {
  let totalPages = 0;
  document.querySelectorAll('.ocr-year-cb:checked').forEach(cb => totalPages += parseInt(cb.dataset.pages));
  const engine = getActiveChip('ocr-group');
  const secsPerPage = engine === 'surya' ? 30 : engine === 'both' ? 32 : 2;
  const estSecs = totalPages * secsPerPage;
  const estStr = estSecs >= 3600 ? `~${(estSecs/3600).toFixed(1)}h` : estSecs >= 60 ? `~${Math.round(estSecs/60)}min` : `~${estSecs}s`;
  $('ocr-estimate').textContent = totalPages > 0
    ? `${totalPages} pages selected · Estimated ${estStr} (${engine} engine)`
    : 'No years selected.';
}

async function runOCRManager() {
  const lccn = val('lccn');
  if (!lccn) { alert('Enter LCCN first'); return; }
  const ocrMode = getActiveChip('ocr-group');
  if (ocrMode === 'none') { alert('Select an OCR engine first'); return; }
  const checkedYears = [...document.querySelectorAll('.ocr-year-cb:checked')].map(cb => cb.dataset.year);
  if (!checkedYears.length) { alert('Select at least one year'); return; }
  let output = val('output');
  if (output === 'downloads') { output = `downloads/${lccn}`; $('output').value = output; }
  localStorage.setItem(`output_${lccn}`, output);
  clearLog();
  setRunning(true);
  const resp = await fetch('/api/download/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      lccn,
      source: 'loc',
      output,
      ocr: ocrMode,
      ocr_batch: true,
      years: checkedYears.join(','),
      ocr_date: $('ocr-date').value.trim() || null,
      force_ocr: $('ocr-mgr-force').checked
    })
  });
  const res = await resp.json();
  if (!res.ok) { setRunning(false); alert(res.error || res.message); }
}

async function scanDownloads() {
  const output = val('output');
  if (!output) return;
  const panel = $('downloads-panel');
  const summary = $('downloads-summary');
  const ocrPanel = $('ocr-manager-panel');
  // Always show both panels so users know they exist
  panel.style.display = 'block';
  ocrPanel.style.display = 'block';
  try {
    const resp = await fetch(`/api/metadata?output=${encodeURIComponent(output)}`);
    const data = await resp.json();
    if (!data.found || !data.total_issues) {
      summary.innerHTML = '<span style="color:var(--muted)">No downloads found in this output directory. Download some issues first.</span>';
      return;
    }
    const yearKeys = Object.keys(data.years);
    const yearRange = yearKeys.length > 0 ? `${yearKeys[0]}\u2013${yearKeys[yearKeys.length - 1]}` : 'N/A';
    let html = `<div style="margin-bottom:8px"><strong>${data.title || data.lccn}</strong> &bull; ${yearRange} &bull; ${data.total_issues} issue${data.total_issues !== 1 ? 's' : ''} &bull; ${data.total_pages} page${data.total_pages !== 1 ? 's' : ''}</div>`;
    html += '<div style="display:flex; flex-wrap:wrap; gap:6px">';
    for (const [year, info] of Object.entries(data.years)) {
      html += `<span style="background:var(--card); border:1px solid var(--border); border-radius:6px; padding:2px 8px; font-size:0.75rem">${year} <span style="color:var(--muted)">${info.issues} iss · ${info.pages} pg</span></span>`;
    }
    html += '</div>';
    summary.innerHTML = html;
    const badge = $('downloads-badge');
    badge.textContent = `${data.total_issues} issue${data.total_issues !== 1 ? 's' : ''} \u00B7 ${data.total_pages} pg`;
    badge.style.display = 'inline';
  } catch (e) {
    summary.innerHTML = '<span style="color:var(--muted)">No downloads found in this output directory. Download some issues first.</span>';
  }
}

// Restore saved output directory for the current LCCN (if any)
(function restoreSavedOutput() {
  const lccn = val('lccn');
  if (lccn) {
    const saved = localStorage.getItem(`output_${lccn}`);
    if (saved) $('output').value = saved;
  }
})();

// Connect SSE on page load
connectSSE();
// Auto-scan for existing downloads
setTimeout(scanDownloads, 500);

// ── Version & Update Check ───────────────────────────────────────────────────
async function checkVersion() {
  try {
    const resp = await fetch('/api/version');
    const data = await resp.json();
    if (data.version) $('version-label').textContent = `v${data.version}`;
  } catch(e) {}
}

async function checkForUpdate() {
  try {
    const resp = await fetch('/api/update/check');
    const data = await resp.json();
    if (data.update_available) {
      $('update-text').innerHTML = `<strong>Update available:</strong> ${data.current} &rarr; ${data.latest}` + (data.name && data.name !== data.latest ? ` &mdash; ${data.name}` : '');
      $('update-banner').style.display = 'block';
      $('update-banner').dataset.url = data.zipball_url || '';
    }
  } catch(e) {}
}

async function applyUpdate() {
  const btn = $('btn-update');
  btn.disabled = true;
  btn.textContent = 'Updating...';
  try {
    const resp = await fetch('/api/update/apply', {method: 'POST'});
    const data = await resp.json();
    if (data.ok) {
      $('update-text').innerHTML = `<strong>Updated to v${data.message}!</strong> Restart PaperRouter to use the new version.`;
      btn.style.display = 'none';
    } else {
      $('update-text').innerHTML = `<strong>Update failed:</strong> ${data.error || 'Unknown error'}`;
      btn.textContent = 'Retry';
      btn.disabled = false;
    }
  } catch(e) {
    $('update-text').innerHTML = `<strong>Update failed:</strong> ${e.message}`;
    btn.textContent = 'Retry';
    btn.disabled = false;
  }
}

checkVersion();
setTimeout(checkForUpdate, 1000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    if not DOWNLOADER_SCRIPT.exists():
        print(f"ERROR: downloader.py not found at {DOWNLOADER_SCRIPT}")
        sys.exit(1)

    import socket

    chosen_port = None
    for port in PORT_CANDIDATES:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
            chosen_port = port
            break
        except OSError:
            print(f"Port {port} is unavailable, trying next...")

    if chosen_port is None:
        print(f"ERROR: All candidate ports {PORT_CANDIDATES} are in use.")
        sys.exit(1)

    # Open browser after a short delay to let the server start
    def open_browser():
        time.sleep(1.0)
        webbrowser.open(f"http://localhost:{chosen_port}")

    threading.Thread(target=open_browser, daemon=True).start()

    print(f"Starting web GUI on http://localhost:{chosen_port}")
    print("Press Ctrl+C to stop the server.")

    app.run(host="127.0.0.1", port=chosen_port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
