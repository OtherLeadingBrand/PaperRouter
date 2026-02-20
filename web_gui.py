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
PROGRESS_RE = re.compile(r'\[(\d+)/(\d+)\]\s+Processing')
FOUND_ISSUES_RE = re.compile(r'Found\s+(\d+)\s+issues')
EMPTY_WARNING_RE = re.compile(r'No issues found matching criteria')

PORT_CANDIDATES = [5000, 5001, 8080, 5005, 8081, 8082, 8083, 8084]


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
        self.progress = {"current": 0, "total": 0}
        self._stopped = False   # set by stop() so _reader knows to exit quietly

    def start(self, cmd, use_harness=False):
        with self.lock:
            if self.is_running:
                return False, "Download already in progress"
            self.log_lines.clear()
            self.progress = {"current": 0, "total": 0}
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
        m = PROGRESS_RE.search(line)
        if m:
            self.progress = {"current": int(m.group(1)), "total": int(m.group(2))}
        else:
            m = FOUND_ISSUES_RE.search(line)
            if m:
                self.progress = {"current": 0, "total": int(m.group(1))}
            elif EMPTY_WARNING_RE.search(line):
                self.progress = {"current": 0, "total": 0}
                m = True
        
        if m:
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
  <h1>PaperRouter</h1>
  <p class="subtitle">Historical Newspaper Archive & OCR Suite</p>
</header>

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

<!-- Controls -->
<div class="card" style="margin-bottom:32px">
  <div style="display:flex; justify-content:space-between; align-items:center">
    <div style="display:flex; gap:12px">
      <button id="btn-start" class="btn-primary" onclick="startDownload()" style="min-width:140px">
        Start Download
      </button>
      <button id="btn-stop" class="btn-danger" onclick="stopDownload()" disabled>Stop</button>
    </div>
    <div style="display:flex; gap:12px">
      <button id="btn-ocr-batch" class="btn-secondary" onclick="startOCRBatch()">Retroactive OCR</button>
      <button class="btn-secondary" onclick="clearLog()">Clear Log</button>
    </div>
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

<div class="card log-card">
  <div class="log-header">
    <h2 style="margin:0">Process Console</h2>
    <div style="font-size:0.75rem; color:var(--muted)">Streamed from downloader.py</div>
  </div>
  <div id="log"></div>
</div>

<script>
const $ = id => document.getElementById(id);
const val = id => $(id).value.trim();

// Chip Logic
document.querySelectorAll('.toggle-row .chip').forEach(chip => {
  chip.onclick = function() {
    this.parentElement.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    this.classList.add('active');
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
  $('btn-ocr-batch').disabled = running;
  $('status-bar').textContent = running ? 'Process active' : 'Process idle';
  if (running) {
    $('progress-fill').classList.add('indeterminate');
    $('progress-stats').textContent = 'Initialising...';
  } else {
    $('progress-fill').classList.remove('indeterminate');
  }
}

function updateProgress(current, total) {
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  $('progress-fill').classList.remove('indeterminate');
  $('progress-fill').style.width = pct + '%';
  $('progress-stats').textContent = total > 0 ? `Processing ${current} of ${total}` : 'No issues found';
}

function appendLog(text) {
  const log = $('log');
  const needsScroll = log.scrollTop + log.offsetHeight >= log.scrollHeight - 20;
  log.textContent += text;
  if (needsScroll) log.scrollTop = log.scrollHeight;
}

function clearLog() { $('log').textContent = ''; }

function connectSSE() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource('/api/download/stream');

  eventSource.addEventListener('log', e => {
    const data = JSON.parse(e.data);
    appendLog(data.line);
  });

  eventSource.addEventListener('progress', e => {
    const data = JSON.parse(e.data);
    updateProgress(data.current, data.total);
  });

  eventSource.addEventListener('status', e => {
    const data = JSON.parse(e.data);
    setRunning(data.running);
  });

  eventSource.addEventListener('done', e => {
    const data = JSON.parse(e.data);
    setRunning(false);
    if (data.status === 'success') {
      updateProgress(data.progress.total, data.progress.total);
      $('progress-stats').textContent = data.progress.total > 0 ? 'Download complete' : 'Discovery finished';
    } else {
      $('progress-stats').textContent = data.status === 'stopped' ? 'Process stopped' : 'Process error';
    }
  });

  eventSource.onerror = () => {
    setTimeout(() => { if (eventSource.readyState === EventSource.CLOSED) connectSSE(); }, 2000);
  };
}

async function startDownload() {
  const lccn = val('lccn');
  if (!lccn) { alert('Enter LCCN first'); return; }
  clearLog();
  setRunning(true);
  const resp = await fetch('/api/download/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      lccn,
      source: 'loc',
      output: val('output'),
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
  clearLog();
  setRunning(true);
  const resp = await fetch('/api/download/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      lccn,
      source: 'loc',
      output: val('output'),
      ocr: getActiveChip('ocr-group') === 'none' ? 'loc' : getActiveChip('ocr-group'),
      ocr_batch: true
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
      if ($('output').value === 'downloads' || $('output').value.startsWith('downloads/')) {
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

// Connect SSE on page load
connectSSE();
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
