#!/usr/bin/env python3
"""
LOC Newspaper Downloader - Graphical Interface
A beginner-friendly GUI for downloading historical newspapers
from the Library of Congress Chronicling America collection.
"""

import sys
import os
import re
import json
import subprocess
import threading
import queue
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, scrolledtext, messagebox
except ImportError:
    print("ERROR: tkinter is not available.")
    print("On Windows, tkinter should come with Python by default.")
    print("If you're on Linux, install it with: sudo apt-get install python3-tk")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).parent
DOWNLOADER_SCRIPT = SCRIPT_DIR / "downloader.py"
HARNESS_SCRIPT = SCRIPT_DIR / "harness.py"
LCCN_PATTERN = re.compile(r'^[a-z]{1,3}\d{8,10}$')

def _needs_harness(ocr_mode: str) -> bool:
    """Return True when the OCR mode includes Surya (memory-intensive AI)."""
    return ocr_mode in ('surya', 'both')


class DownloaderGUI:
    """GUI for the LOC Newspaper Downloader."""

    POLL_INTERVAL = 100  # ms

    def __init__(self, root):
        self.root = root
        self.root.title("LOC Newspaper Downloader")
        self.root.geometry("780x720")
        self.root.resizable(True, True)

        self.download_process = None
        self.is_downloading = False
        self.output_queue = queue.Queue()

        # Progress tracking (parsed from log output)
        self._total_issues = 0
        self._current_issue = 0

        self._create_widgets()
        self._poll_output_queue()

    # ------------------------------------------------------------------
    # Widget creation
    # ------------------------------------------------------------------

    def _create_widgets(self):
        # Title bar
        title_frame = ttk.Frame(self.root, padding="10")
        title_frame.pack(fill=tk.X)

        ttk.Label(
            title_frame,
            text="LOC Newspaper Downloader",
            font=("Arial", 16, "bold"),
        ).pack()
        ttk.Label(
            title_frame,
            text="Download historical newspapers from the Library of Congress",
            font=("Arial", 9),
        ).pack()

        # ---- Newspaper selection ----
        newspaper_frame = ttk.LabelFrame(
            self.root, text="Newspaper Selection", padding="10"
        )
        newspaper_frame.pack(fill=tk.X, padx=10, pady=5)

        # LCCN
        ttk.Label(newspaper_frame, text="LCCN:").grid(
            row=0, column=0, sticky=tk.W, pady=5
        )
        self.lccn_var = tk.StringVar(value="sn87080287")
        lccn_entry = ttk.Entry(
            newspaper_frame, textvariable=self.lccn_var, width=25
        )
        lccn_entry.grid(row=0, column=1, padx=5, pady=5, sticky=tk.W)

        ttk.Button(
            newspaper_frame, text="Look Up", command=self._lookup_lccn
        ).grid(row=0, column=2, padx=5, pady=5)

        ttk.Label(
            newspaper_frame,
            text="(e.g. sn87080287, sn83045462)",
            foreground="gray",
        ).grid(row=0, column=3, padx=5, sticky=tk.W)

        # Source
        ttk.Label(newspaper_frame, text="Source:").grid(
            row=1, column=0, sticky=tk.W, pady=5
        )
        self.source_var = tk.StringVar(value="loc")
        source_combo = ttk.Combobox(
            newspaper_frame, textvariable=self.source_var,
            values=["loc"], state="readonly", width=10
        )
        source_combo.grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)
        ttk.Label(newspaper_frame, text="Library of Congress", font=("Arial", 8, "italic")).grid(row=1, column=2, sticky=tk.W)

        # Search
        ttk.Label(newspaper_frame, text="Search:").grid(
            row=2, column=0, sticky=tk.W, pady=5
        )
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(
            newspaper_frame, textvariable=self.search_var, width=35
        )
        search_entry.grid(row=2, column=1, columnspan=2, padx=5, pady=5, sticky=tk.W)
        search_entry.bind("<Return>", lambda e: self._search_newspapers())

        ttk.Button(
            newspaper_frame, text="Search", command=self._search_newspapers
        ).grid(row=2, column=3, padx=5, pady=5, sticky=tk.W)

        # Search results list
        self.results_list = tk.Listbox(
            newspaper_frame, height=4, font=("Courier New", 9)
        )
        self.results_list.grid(
            row=3, column=0, columnspan=4, sticky=tk.EW, padx=5, pady=5
        )
        self.results_list.bind("<<ListboxSelect>>", self._on_result_select)
        newspaper_frame.columnconfigure(1, weight=1)

        self._search_results = []  # list of LCCN strings

        # ---- Download options ----
        options_frame = ttk.LabelFrame(
            self.root, text="Download Options", padding="10"
        )
        options_frame.pack(fill=tk.X, padx=10, pady=5)

        # Output directory
        ttk.Label(options_frame, text="Output Folder:").grid(
            row=0, column=0, sticky=tk.W, pady=5
        )
        self.output_var = tk.StringVar(value="downloads")
        ttk.Entry(
            options_frame, textvariable=self.output_var, width=40
        ).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(
            options_frame, text="Browse...", command=self._browse_output
        ).grid(row=0, column=2, padx=5, pady=5)

        # Year selection
        ttk.Label(options_frame, text="Years:").grid(
            row=1, column=0, sticky=tk.W, pady=5
        )
        years_frame = ttk.Frame(options_frame)
        years_frame.grid(row=1, column=1, columnspan=2, sticky=tk.W, pady=5)

        self.year_mode = tk.StringVar(value="all")
        ttk.Radiobutton(
            years_frame, text="All available",
            variable=self.year_mode, value="all",
            command=self._update_year_state,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            years_frame, text="Custom:",
            variable=self.year_mode, value="custom",
            command=self._update_year_state,
        ).pack(side=tk.LEFT, padx=(20, 5))

        self.years_var = tk.StringVar(value="1900-1905")
        self.years_entry = ttk.Entry(years_frame, textvariable=self.years_var, width=20)
        self.years_entry.pack(side=tk.LEFT)
        self.years_entry.config(state="disabled")
        ttk.Label(
            years_frame, text='(e.g. "1900-1905" or "1900,1903")'
        ).pack(side=tk.LEFT, padx=5)

        # Speed + checkboxes row
        cb_frame = ttk.Frame(options_frame)
        cb_frame.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=2)

        ttk.Label(cb_frame, text="Speed:").pack(side=tk.LEFT)
        self.speed_var = tk.StringVar(value="safe")
        ttk.Radiobutton(
            cb_frame, text="Safe (15 s)", variable=self.speed_var, value="safe"
        ).pack(side=tk.LEFT, padx=(5, 10))
        ttk.Radiobutton(
            cb_frame, text="Standard (4 s)", variable=self.speed_var, value="standard"
        ).pack(side=tk.LEFT, padx=(0, 15))

        self.verbose_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            cb_frame, text="Verbose", variable=self.verbose_var
        ).pack(side=tk.LEFT, padx=(0, 15))

        self.retry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            cb_frame, text="Retry failed", variable=self.retry_var
        ).pack(side=tk.LEFT)

        # OCR row
        ocr_row = ttk.Frame(options_frame)
        ocr_row.grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=2)

        ttk.Label(ocr_row, text="OCR:").pack(side=tk.LEFT)
        self.ocr_var = tk.StringVar(value="none")
        ttk.Radiobutton(
            ocr_row, text="None", variable=self.ocr_var, value="none"
        ).pack(side=tk.LEFT, padx=(5, 10))
        ttk.Radiobutton(
            ocr_row, text="LOC (Fast)", variable=self.ocr_var, value="loc"
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(
            ocr_row, text="Surya (AI)", variable=self.ocr_var, value="surya"
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(
            ocr_row, text="Both", variable=self.ocr_var, value="both"
        ).pack(side=tk.LEFT)

        # ---- Buttons ----
        btn_frame = ttk.Frame(self.root, padding="5 10")
        btn_frame.pack(fill=tk.X)

        self.start_btn = ttk.Button(
            btn_frame, text="Start Download", command=self._start_download
        )
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(
            btn_frame, text="Stop", command=self._stop_download, state="disabled"
        )
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        ttk.Button(
            btn_frame, text="Clear Log", command=self._clear_output
        ).pack(side=tk.LEFT, padx=5)

        self.ocr_batch_btn = ttk.Button(
            btn_frame, text="OCR Batch", command=self._run_ocr_batch
        )
        self.ocr_batch_btn.pack(side=tk.RIGHT, padx=5)

        # ---- Progress bar ----
        prog_frame = ttk.Frame(self.root, padding="0 2 10 2")
        prog_frame.pack(fill=tk.X)

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            prog_frame, variable=self.progress_var,
            maximum=100, mode='determinate',
        )
        self.progress_bar.pack(fill=tk.X, padx=10)

        self.progress_label_var = tk.StringVar(value="")
        ttk.Label(
            prog_frame, textvariable=self.progress_label_var, anchor=tk.CENTER
        ).pack(fill=tk.X)

        # ---- Log output ----
        log_frame = ttk.LabelFrame(self.root, text="Log", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 5))

        self.output_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, height=12, font=("Courier New", 9)
        )
        self.output_text.pack(fill=tk.BOTH, expand=True)

        # ---- Status bar ----
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(
            self.root, textvariable=self.status_var, relief=tk.SUNKEN
        ).pack(fill=tk.X, side=tk.BOTTOM, padx=2, pady=2)

    # ------------------------------------------------------------------
    # Thread-safe output handling
    # ------------------------------------------------------------------

    def _poll_output_queue(self):
        try:
            while True:
                msg_type, msg_data = self.output_queue.get_nowait()

                if msg_type == 'output':
                    self.output_text.insert(tk.END, msg_data)
                    self.output_text.see(tk.END)
                    self._parse_progress(msg_data)
                elif msg_type == 'status':
                    self.status_var.set(msg_data)
                elif msg_type == 'done':
                    self.is_downloading = False
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.download_process = None
                    if msg_data == 'success':
                        self.progress_var.set(100)
                        self.progress_label_var.set("Complete")
                        messagebox.showinfo("Done", "Download completed successfully!")
                elif msg_type == 'error':
                    messagebox.showerror("Error", f"An error occurred:\n{msg_data}")
                elif msg_type == 'search_results':
                    self._populate_search_results(msg_data)
                elif msg_type == 'info_result':
                    self._show_info_result(msg_data)

        except queue.Empty:
            pass

        self.root.after(self.POLL_INTERVAL, self._poll_output_queue)

    # Progress pattern:  "[3/150] Processing ..."
    _PROGRESS_RE = re.compile(r'\[(\d+)/(\d+)\]\s+Processing')

    def _parse_progress(self, line: str):
        """Extract issue progress from log output and update the bar."""
        m = self._PROGRESS_RE.search(line)
        if m:
            current = int(m.group(1))
            total = int(m.group(2))
            self._current_issue = current
            self._total_issues = total
            pct = (current / total) * 100 if total else 0
            self.progress_var.set(pct)
            self.progress_label_var.set(f"Issue {current} of {total}")

    # ------------------------------------------------------------------
    # Newspaper search / lookup (uses --json for reliable parsing)
    # ------------------------------------------------------------------

    def _search_newspapers(self):
        query = self.search_var.get().strip()
        if not query:
            messagebox.showwarning("Search", "Please enter a search term.")
            return

        self.results_list.delete(0, tk.END)
        self.results_list.insert(tk.END, "Searching...")
        self.status_var.set("Searching...")

        threading.Thread(
            target=self._search_worker, args=(query,), daemon=True
        ).start()

    def _search_worker(self, query):
        try:
            cmd = [
                sys.executable, str(DOWNLOADER_SCRIPT),
                "--source", self.source_var.get(),
                "--search", query, "--json",
            ]
            creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
                creationflags=creation_flags,
            )
            self.output_queue.put(('search_results', result.stdout))
            self.output_queue.put(('status', 'Ready'))
        except Exception as e:
            self.output_queue.put(('search_results', json.dumps([])))
            self.output_queue.put(('status', 'Ready'))

    def _populate_search_results(self, output: str):
        self.results_list.delete(0, tk.END)
        self._search_results = []

        try:
            results = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            # Fallback: not JSON, show raw
            self.results_list.insert(tk.END, "No results (or parse error)")
            return

        if not results:
            self.results_list.insert(tk.END, "No newspapers found.")
            return

        for r in results:
            lccn = r.get('lccn', '')
            title = r.get('title', 'Unknown')[:42]
            place = r.get('place', '')[:22]
            dates = r.get('dates', '')
            line = f"{lccn:<15} {title:<44} {place:<24} {dates}"
            self._search_results.append(lccn)
            self.results_list.insert(tk.END, line)

    def _on_result_select(self, event):
        selection = self.results_list.curselection()
        if selection and selection[0] < len(self._search_results):
            lccn = self._search_results[selection[0]]
            self.lccn_var.set(lccn)

    def _lookup_lccn(self):
        lccn = self.lccn_var.get().strip()
        if not lccn:
            messagebox.showwarning("Look Up", "Please enter an LCCN.")
            return

        self.results_list.delete(0, tk.END)
        self.results_list.insert(tk.END, f"Looking up {lccn}...")
        self.status_var.set("Looking up...")

        threading.Thread(
            target=self._info_worker, args=(lccn,), daemon=True
        ).start()

    def _info_worker(self, lccn):
        try:
            cmd = [
                sys.executable, str(DOWNLOADER_SCRIPT),
                "--source", self.source_var.get(),
                "--info", lccn, "--json",
            ]
            creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
                creationflags=creation_flags,
            )
            self.output_queue.put(('info_result', result.stdout))
            self.output_queue.put(('status', 'Ready'))
        except Exception as e:
            self.output_queue.put(('info_result', '{}'))
            self.output_queue.put(('status', 'Ready'))

    def _show_info_result(self, output: str):
        self.results_list.delete(0, tk.END)

        try:
            info = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            self.results_list.insert(tk.END, "Lookup failed (parse error)")
            return

        if not info:
            self.results_list.insert(tk.END, "No newspaper found for that LCCN.")
            return

        self.results_list.insert(tk.END, f"Title:   {info.get('title', '?')}")
        self.results_list.insert(tk.END, f"LCCN:    {info.get('lccn', '?')}")
        self.results_list.insert(tk.END, f"Place:   {info.get('place', '?')}")
        sy, ey = info.get('start_year'), info.get('end_year')
        if sy and ey:
            self.results_list.insert(tk.END, f"Dates:   {sy}-{ey}")

    # ------------------------------------------------------------------
    # Download controls
    # ------------------------------------------------------------------

    def _update_year_state(self):
        if self.year_mode.get() == "all":
            self.years_entry.config(state="disabled")
        else:
            self.years_entry.config(state="normal")

    def _run_ocr_batch(self):
        if self.is_downloading:
            messagebox.showwarning("Busy", "Cannot run OCR batch while downloading!")
            return

        lccn = self.lccn_var.get().strip()
        if not lccn:
            messagebox.showerror("Error", "Please enter an LCCN to process.")
            return

        output_dir = self.output_var.get().strip() or "downloads"
        ocr_mode = self.ocr_var.get()
        if ocr_mode == "none":
            ocr_mode = "loc"

        use_harness = _needs_harness(ocr_mode)
        launcher = str(HARNESS_SCRIPT) if use_harness else str(DOWNLOADER_SCRIPT)
        cmd = [
            sys.executable, launcher,
            "--lccn", lccn,
            "--output", output_dir,
            "--ocr", ocr_mode,
            "--ocr-batch"
        ]
        self._using_harness = use_harness
        
        if self.verbose_var.get():
            cmd.append("--verbose")

        self._total_issues = 0
        self._current_issue = 0
        self.progress_var.set(0)
        self.progress_label_var.set("Starting OCR Batch...")

        self.is_downloading = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.ocr_batch_btn.config(state="disabled")
        self.status_var.set("Running OCR batch...")

        threading.Thread(
            target=self._run_download, args=(cmd,), daemon=True
        ).start()

    def _browse_output(self):
        directory = filedialog.askdirectory(
            title="Select Output Directory",
            initialdir=self.output_var.get(),
        )
        if directory:
            self.output_var.set(directory)

    def _clear_output(self):
        self.output_text.delete(1.0, tk.END)

    def _start_download(self):
        if self.is_downloading:
            messagebox.showwarning("Busy", "Download is already in progress!")
            return

        lccn = self.lccn_var.get().strip()
        if not lccn:
            messagebox.showerror("Error", "Please enter an LCCN identifier.")
            return

        if not LCCN_PATTERN.match(lccn):
            if not messagebox.askyesno(
                "LCCN Format",
                f"'{lccn}' doesn't look like a standard LCCN "
                f"(expected e.g. 'sn87080287').\n\n"
                f"Continue anyway?",
            ):
                return

        output_dir = self.output_var.get().strip() or "downloads"

        if not DOWNLOADER_SCRIPT.exists():
            messagebox.showerror(
                "Error",
                f"downloader.py not found:\n{DOWNLOADER_SCRIPT}\n\n"
                "Make sure it is in the same folder as gui.py.",
            )
            return

        ocr_mode = self.ocr_var.get()
        use_harness = _needs_harness(ocr_mode)

        if use_harness:
            # Route through harness for memory protection when Surya is active
            cmd = [
                sys.executable, str(HARNESS_SCRIPT),
                "--source", self.source_var.get(),
                "--lccn", lccn,
                "--output", output_dir,
                "--speed", self.speed_var.get(),
            ]
        else:
            cmd = [
                sys.executable, str(DOWNLOADER_SCRIPT),
                "--source", self.source_var.get(),
                "--lccn", lccn,
                "--output", output_dir,
                "--speed", self.speed_var.get(),
            ]

        if self.year_mode.get() == "custom":
            years = self.years_var.get().strip()
            if years:
                cmd.extend(["--years", years])

        if self.verbose_var.get():
            cmd.append("--verbose")
        if self.retry_var.get():
            cmd.append("--retry-failed")

        if ocr_mode != "none":
            cmd.extend(["--ocr", ocr_mode])

        # Reset progress
        self._total_issues = 0
        self._current_issue = 0
        self.progress_var.set(0)
        self.progress_label_var.set("")

        self.is_downloading = True
        self._using_harness = use_harness
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        status_msg = "Downloading (with memory protection)..." if use_harness else "Downloading..."
        self.status_var.set(status_msg)

        self.output_queue.put(('output', f"LCCN: {lccn}\n"))
        if use_harness:
            self.output_queue.put(('output', "[Memory protection active — Surya AI OCR monitored]\n"))
        self.output_queue.put(('output', f"Command: {' '.join(cmd)}\n"))
        self.output_queue.put(('output', "=" * 70 + "\n\n"))

        threading.Thread(
            target=self._run_download, args=(cmd,), daemon=True
        ).start()

    def _run_download(self, cmd):
        try:
            creation_flags = (
                subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            self.download_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                creationflags=creation_flags,
            )

            for line in self.download_process.stdout:
                self.output_queue.put(('output', line))

            self.download_process.wait()

            if self.download_process.returncode == 0:
                self.output_queue.put(('output', "\n" + "=" * 70 + "\n"))
                self.output_queue.put(('output', "Download completed!\n"))
                self.output_queue.put(('status', "Complete"))
                self.output_queue.put(('done', 'success'))
            else:
                self.output_queue.put(('output', "\n" + "=" * 70 + "\n"))
                self.output_queue.put((
                    'output',
                    f"Exited with code {self.download_process.returncode}\n",
                ))
                self.output_queue.put(('status', "Stopped"))
                self.output_queue.put(('done', 'stopped'))

        except Exception as e:
            self.output_queue.put(('output', f"\nError: {e}\n"))
            self.output_queue.put(('status', "Error"))
            self.output_queue.put(('error', str(e)))
            self.output_queue.put(('done', 'error'))

    def _stop_download(self):
        if self.download_process and self.is_downloading:
            if messagebox.askyesno(
                "Confirm Stop",
                "Stop the download?\n\nProgress is saved; you can resume later.",
            ):
                if getattr(self, '_using_harness', False) and HARNESS_SCRIPT.exists():
                    # Use harness --kill to cleanly terminate the full process tree
                    subprocess.Popen(
                        [sys.executable, str(HARNESS_SCRIPT), "--kill"],
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
                    )
                else:
                    self.download_process.terminate()
                self.output_queue.put(('output', "\n\nStopped by user.\n"))
                self.output_queue.put(('status', "Stopped"))


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    if not DOWNLOADER_SCRIPT.exists():
        tmp = tk.Tk()
        tmp.withdraw()
        messagebox.showerror(
            "Error",
            f"Required file not found: downloader.py\n\n"
            f"It must be in the same folder as gui.py:\n{SCRIPT_DIR}",
        )
        tmp.destroy()
        sys.exit(1)

    try:
        import requests  # noqa: F401
    except ImportError:
        tmp = tk.Tk()
        tmp.withdraw()
        if messagebox.askyesno(
            "Missing Dependency",
            "The 'requests' library is required.\n\nInstall it now?",
        ):
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "requests"]
                )
            except subprocess.CalledProcessError:
                messagebox.showerror(
                    "Error",
                    "Failed to install requests.\n\n"
                    "Run manually: pip install requests",
                )
                tmp.destroy()
                sys.exit(1)
        else:
            tmp.destroy()
            sys.exit(1)
        tmp.destroy()

    root = tk.Tk()
    try:
        style = ttk.Style()
        themes = style.theme_names()
        if 'vista' in themes:
            style.theme_use('vista')
        elif 'clam' in themes:
            style.theme_use('clam')
    except Exception:
        pass

    DownloaderGUI(root)
    root.mainloop()


if __name__ == '__main__':
    main()
