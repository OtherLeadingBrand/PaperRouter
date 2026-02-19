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
import tkinter as tk
from tkinter import filedialog

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

PORT = 5000


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
                               creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)
                
                # Double-check for harness.py's specific PID file if it exists
                if HARNESS_SCRIPT.exists() and (SCRIPT_DIR / ".harness.pid").exists():
                    try:
                        h_pid = int((SCRIPT_DIR / ".harness.pid").read_text().strip())
                        subprocess.run(['taskkill', '/F', '/T', '/PID', str(h_pid)],
                                       creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)
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
    import tkinter as tk
    from tkinter import filedialog
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
<title>PaperRouter</title>
<style>
  :root {
    --bg: #f7f7f8;
    --card: #fff;
    --border: #ddd;
    --primary: #2563eb;
    --primary-hover: #1d4ed8;
    --danger: #dc2626;
    --danger-hover: #b91c1c;
    --text: #1e293b;
    --muted: #64748b;
    --radius: 6px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5;
    max-width: 860px; margin: 0 auto; padding: 24px 16px;
  }
  h1 { font-size: 1.5rem; margin-bottom: 2px; }
  .subtitle { color: var(--muted); font-size: 0.9rem; margin-bottom: 20px; }
  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 16px; margin-bottom: 16px;
  }
  .card h2 { font-size: 0.95rem; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.05em; margin-bottom: 12px; }
  label { display: block; font-size: 0.85rem; font-weight: 600; margin-bottom: 4px; }
  input[type="text"], select {
    width: 100%; padding: 7px 10px; border: 1px solid var(--border);
    border-radius: var(--radius); font-size: 0.9rem;
  }
  input[type="text"]:focus, select:focus {
    outline: none; border-color: var(--primary); box-shadow: 0 0 0 2px rgba(37,99,235,0.15);
  }
  .row { display: flex; gap: 12px; align-items: end; flex-wrap: wrap; }
  .row > * { flex: 1; min-width: 0; }
  .row > .narrow { flex: 0 0 auto; }
  .radio-group, .check-group {
    display: flex; gap: 14px; flex-wrap: wrap; align-items: center;
    font-size: 0.85rem; padding: 4px 0;
  }
  .radio-group label, .check-group label {
    display: inline-flex; align-items: center; gap: 4px;
    font-weight: normal; cursor: pointer;
  }
  button {
    padding: 8px 18px; border: none; border-radius: var(--radius);
    font-size: 0.9rem; font-weight: 600; cursor: pointer; transition: background 0.15s;
  }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover { background: var(--primary-hover); }
  .btn-primary:disabled { background: #93c5fd; cursor: not-allowed; }
  .btn-danger { background: var(--danger); color: #fff; }
  .btn-danger:hover { background: var(--danger-hover); }
  .btn-danger:disabled { background: #fca5a5; cursor: not-allowed; }
  .btn-secondary { background: #e2e8f0; color: var(--text); }
  .btn-secondary:hover { background: #cbd5e1; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .actions .spacer { flex: 1; }

  /* Progress */
  .progress-wrap { margin-bottom: 16px; }
  .progress-bar-bg {
    height: 8px; background: #e2e8f0; border-radius: 4px; overflow: hidden;
  }
  .progress-bar-fill {
    height: 100%; background: var(--primary); width: 0%;
    transition: width 0.3s ease; border-radius: 4px;
  }
  .progress-bar-fill.indeterminate {
    width: 30% !important;
    animation: indeterminate 1.4s ease-in-out infinite;
  }
  @keyframes indeterminate {
    0% { transform: translateX(-100%); }
    100% { transform: translateX(430%); }
  }
  .progress-text {
    font-size: 0.8rem; color: var(--muted); text-align: center; margin-top: 4px;
  }

  /* Log */
  #log {
    background: #1e293b; color: #e2e8f0; font-family: "Cascadia Code", "Fira Code",
    "Consolas", monospace; font-size: 0.8rem; line-height: 1.6;
    padding: 12px; border-radius: var(--radius); height: 300px;
    overflow-y: auto; white-space: pre-wrap; word-break: break-all;
  }

  /* Search results */
  #search-results {
    max-height: 120px; overflow-y: auto; font-size: 0.85rem;
    border: 1px solid var(--border); border-radius: var(--radius);
    display: none;
  }
  #search-results .result-item {
    padding: 6px 10px; cursor: pointer; border-bottom: 1px solid #f1f5f9;
  }
  #search-results .result-item:hover { background: #f1f5f9; }
  #search-results .result-item:last-child { border-bottom: none; }

  .status-bar {
    font-size: 0.8rem; color: var(--muted); text-align: center; padding: 6px;
  }
</style>
</head>
<body>

<h1>PaperRouter</h1>
<p class="subtitle">A robust multi-source newspaper downloader and OCR suite</p>

<!-- Newspaper Selection -->
<div class="card">
  <h2>Newspaper Selection</h2>
  <div class="row" style="margin-bottom:10px">
    <div>
      <label for="lccn">LCCN</label>
      <input type="text" id="lccn" value="sn87080287" placeholder="e.g. sn87080287">
    </div>
    <div class="narrow">
      <button class="btn-secondary" onclick="lookupLCCN()">Look Up</button>
    </div>
  </div>
  <div class="row" style="margin-bottom:10px">
    <div>
      <label for="search-input">Search newspapers</label>
      <input type="text" id="search-input" placeholder="Search by title..." onkeydown="if(event.key==='Enter')searchNewspapers()">
    </div>
    <div class="narrow">
      <button class="btn-secondary" onclick="searchNewspapers()">Search</button>
    </div>
  </div>
  <div id="search-results"></div>
  <div id="info-display" style="font-size:0.85rem;color:var(--muted);margin-top:6px"></div>
</div>

<!-- Download Options -->
<div class="card">
  <h2>Download Options</h2>
  <div class="row" style="margin-bottom:10px">
    <div style="flex: 2">
      <label for="output">Output folder</label>
      <div class="row" style="gap:5px">
        <input type="text" id="output" value="downloads">
        <button class="btn-secondary narrow" onclick="browseFolder()">Browse</button>
      </div>
    </div>
    <div>
      <label for="source">Source</label>
      <select id="source"><option value="loc">Library of Congress</option></select>
    </div>
  </div>
  <div class="row" style="margin-bottom:8px">
    <div>
      <label>Years <span id="available-years" style="font-weight:normal;color:var(--primary);margin-left:10px"></span></label>
      <div class="radio-group">
        <label><input type="radio" name="year-mode" value="all" checked onchange="toggleYears()"> All available</label>
        <label><input type="radio" name="year-mode" value="custom" onchange="toggleYears()"> Custom:</label>
        <input type="text" id="years" placeholder="1900-1905" style="width:140px;flex:none" disabled>
      </div>
    </div>
  </div>
  <div style="margin-bottom:8px">
    <label>Speed</label>
    <div class="radio-group">
      <label><input type="radio" name="speed" value="safe" checked> Safe (15s)</label>
      <label><input type="radio" name="speed" value="standard"> Standard (4s)</label>
    </div>
  </div>
  <div style="margin-bottom:8px">
    <label>OCR</label>
    <div class="radio-group">
      <label><input type="radio" name="ocr" value="none" checked> None</label>
      <label><input type="radio" name="ocr" value="loc"> LOC (Fast)</label>
      <label><input type="radio" name="ocr" value="surya"> Surya (AI)</label>
      <label><input type="radio" name="ocr" value="both"> Both</label>
    </div>
  </div>
  <div class="check-group">
    <label><input type="checkbox" id="verbose"> Verbose</label>
    <label><input type="checkbox" id="retry-failed"> Retry failed</label>
  </div>
</div>

<!-- Actions -->
<div class="card">
  <div class="actions">
    <button id="btn-start" class="btn-primary" onclick="startDownload()">Start Download</button>
    <button id="btn-stop" class="btn-danger" onclick="stopDownload()" disabled>Stop</button>
    <button class="btn-secondary" onclick="clearLog()">Clear Log</button>
    <div class="spacer"></div>
    <button id="btn-ocr-batch" class="btn-secondary" onclick="startOCRBatch()">OCR Batch</button>
  </div>
</div>

<!-- Progress -->
<div class="progress-wrap">
  <div class="progress-bar-bg"><div id="progress-fill" class="progress-bar-fill"></div></div>
  <div id="progress-text" class="progress-text"></div>
</div>

<!-- Log -->
<div class="card" style="padding:0;overflow:hidden">
  <div id="log"></div>
</div>

<div id="status-bar" class="status-bar">Ready</div>

<script>
const $ = id => document.getElementById(id);
const val = id => $(id).value.trim();
const radio = name => document.querySelector(`input[name="${name}"]:checked`)?.value;

let eventSource = null;

function getOptions() {
  return {
    lccn: val('lccn'),
    source: val('source'),
    output: val('output'),
    speed: radio('speed'),
    ocr: radio('ocr'),
    years: radio('year-mode') === 'custom' ? val('years') : '',
    verbose: $('verbose').checked,
    retry_failed: $('retry-failed').checked,
  };
}

function setRunning(running) {
  $('btn-start').disabled = running;
  $('btn-stop').disabled = !running;
  $('btn-ocr-batch').disabled = running;
  $('status-bar').textContent = running ? 'Downloading...' : 'Ready';
  if (running) {
    $('progress-fill').className = 'progress-bar-fill indeterminate';
    $('progress-fill').style.width = '30%';
    $('progress-text').textContent = 'Connecting...';
  }
}

function updateProgress(current, total) {
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  $('progress-fill').className = 'progress-bar-fill';
  $('progress-fill').style.width = pct + '%';
  $('progress-text').textContent = `Issue ${current} of ${total}`;
}

function appendLog(text) {
  const log = $('log');
  log.textContent += text;
  log.scrollTop = log.scrollHeight;
}

function clearLog() {
  $('log').textContent = '';
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
      if (data.progress.total === 0) {
        $('progress-fill').style.width = '0%';
        $('progress-text').textContent = 'No matching issues found';
        $('status-bar').textContent = 'Discovery complete - 0 issues found';
      } else {
        $('progress-fill').style.width = '100%';
        $('progress-text').textContent = 'Complete';
        $('status-bar').textContent = 'Download complete';
      }
    } else if (data.status === 'stopped') {
      $('progress-text').textContent = 'Stopped';
      $('status-bar').textContent = 'Stopped';
    } else {
      $('progress-text').textContent = 'Error';
      $('status-bar').textContent = 'Error occurred';
    }
  });

  eventSource.onerror = () => {
    // Reconnect after a brief pause
    setTimeout(() => {
      if (eventSource.readyState === EventSource.CLOSED) connectSSE();
    }, 2000);
  };
}

async function startDownload() {
  const opts = getOptions();
  if (!opts.lccn) { alert('Please enter an LCCN.'); return; }
  clearLog();
  setRunning(true);
  const resp = await fetch('/api/download/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(opts),
  });
  const result = await resp.json();
  if (!result.ok) {
    setRunning(false);
    alert(result.error || result.message);
  }
}

async function stopDownload() {
  await fetch('/api/download/stop', {method: 'POST'});
}

async function startOCRBatch() {
  const opts = getOptions();
  if (!opts.lccn) { alert('Please enter an LCCN.'); return; }
  if (opts.ocr === 'none') opts.ocr = 'loc';
  opts.ocr_batch = true;
  clearLog();
  setRunning(true);
  $('progress-text').textContent = 'Starting OCR Batch...';
  const resp = await fetch('/api/download/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(opts),
  });
  const result = await resp.json();
  if (!result.ok) {
    setRunning(false);
    alert(result.error || result.message);
  }
}

async function searchNewspapers() {
  const query = val('search-input');
  if (!query) return;
  const resultsDiv = $('search-results');
  resultsDiv.innerHTML = '<div style="padding:10px;color:var(--muted)">Searching...</div>';
  resultsDiv.style.display = 'block';

  try {
    const resp = await fetch('/api/search', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query, source: val('source')}),
    });
    const results = await resp.json();
    if (results.length > 0) {
      resultsDiv.innerHTML = results.map(r => 
        `<div class="result-item" onclick="selectResult('${r.lccn}')">
          ${r.lccn} &mdash; ${r.title} (${r.place || '?'}) ${r.dates || ''}
         </div>`
      ).join('');
    } else {
      resultsDiv.innerHTML = '<div style="padding:10px;color:var(--muted)">No results found.</div>';
    }
  } catch (e) {
    resultsDiv.innerHTML = '<div style="padding:10px;color:var(--danger)">Search failed.</div>';
  }
}

function selectResult(lccn) {
  $('lccn').value = lccn;
  $('search-results').style.display = 'none';
  lookupLCCN(); // Auto-lookup on selection
}

async function lookupLCCN() {
  const lccn = val('lccn');
  if (!lccn) return;
  
  $('info-display').textContent = 'Looking up newspaper details...';
  const btn = document.querySelectorAll('.btn-secondary')[0];
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '...';

  try {
    const resp = await fetch('/api/lookup', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lccn, source: val('source')}),
    });
    const info = await resp.json();
    if (info && info.title) {
      $('info-display').innerHTML =
        `<strong>${info.title}</strong> (${info.lccn})`;
      if (info.start_year && info.end_year) {
        $('available-years').textContent = `(Available: ${info.start_year}-${info.end_year})`;
      } else {
        $('available-years').textContent = '';
      }
      
      // Suggest output folder name
      const safeTitle = info.title.replace(/[<>:"/\\|?*]/g, '').trim();
      if (safeTitle && ($('output').value === 'downloads' || $('output').value.startsWith('downloads/'))) {
          $('output').value = `downloads/${safeTitle}`;
      }
    } else {
      $('info-display').textContent = 'No newspaper found for that LCCN.';
      $('available-years').textContent = '';
    }
  } catch (e) {
    $('info-display').textContent = 'Lookup failed.';
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

async function browseFolder() {
  const resp = await fetch('/api/browse', {method: 'POST'});
  const data = await resp.json();
  if (data.path) {
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

    # Open browser after a short delay to let the server start
    def open_browser():
        time.sleep(1.0)
        webbrowser.open(f"http://localhost:{PORT}")

    threading.Thread(target=open_browser, daemon=True).start()

    print(f"Starting web GUI on http://localhost:{PORT}")
    print("Press Ctrl+C to stop the server.")

    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
