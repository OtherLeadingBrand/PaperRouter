# Multi-Source Newspaper Downloader

A robust, extensible Python tool to download and OCR historical newspaper editions from major archives, starting with the **Library of Congress [Chronicling America](https://chroniclingamerica.loc.gov/)**.

## Features

- **Pluggable Architecture** -- extensible source system; easily add new newspaper archives
- **Tiered OCR System** -- download pre-existing OCR (Tier 1) or run local AI-powered OCR with reading order reconstruction (Tier 2)
- **Year Filtering** -- high-performance year-filtering at the API level for massive archive speedups
- **Network Resilience** -- automatic retries with exponential backoff and connection management
- **Stop / Resume** -- atomic metadata tracking ensures you never download the same page twice
- **Windows Friendly** -- native batch launchers, GUI mode, and resource-monitored AI worker harness
- **Machine Readable** -- `--json` flags for search/info results, perfect for automation

**New to this tool?** Start with the [QUICKSTART.txt](QUICKSTART.txt) guide -- it walks through everything step by step, including installing Python.

## Quick Start

### 1. Install Dependencies

**Standard mode** (download only):
```bash
pip install -r requirements.txt
```

**AI OCR mode** (Surya-powered local OCR):
```bash
pip install -r requirements.txt surya-ocr pymupdf torch
```

> On Windows you can skip manual setup entirely -- `run.bat` and `run_gui.bat` check for dependencies and install them automatically on first launch.

### 2. Search for a Newspaper

Find a newspaper by name. Every newspaper in the archive has an **LCCN** identifier (e.g. `sn87080287`) that you'll use for downloads.

```bash
python downloader.py --search "Freeland Tribune"
```

Output:
```
Search results for 'Freeland Tribune' (loc):
  sn87080287: Freeland tribune (Freeland, Pa., 1893-19??)
```

Add `--json` for machine-readable output:
```bash
python downloader.py --search "Freeland Tribune" --json
```

### 3. Get Newspaper Details

```bash
python downloader.py --info sn87080287
```

Output:
```
Newspaper: Freeland tribune.
LCCN:      sn87080287
Range:     1893-1918
URL:       https://www.loc.gov/resource/...
```

### 4. Download

```bash
# Download all available issues
python downloader.py --lccn sn87080287

# Download only specific years
python downloader.py --lccn sn87080287 --years 1900-1905

# Download a single test issue
python downloader.py --lccn sn87080287 --max-issues 1
```

### 5. OCR

OCR can run during download or retroactively on existing files.

```bash
# Tier 1: Fetch pre-existing OCR from the LOC API (fast)
python downloader.py --lccn sn87080287 --ocr loc

# Tier 2: Run local AI OCR via Surya (slow, high quality)
python downloader.py --lccn sn87080287 --ocr surya

# Both tiers
python downloader.py --lccn sn87080287 --ocr both

# Retroactive OCR on already-downloaded PDFs
python downloader.py --lccn sn87080287 --ocr loc --ocr-batch
```

## GUI Mode

Launch the graphical interface:

```bash
python gui.py
```

Or double-click `run_gui.bat` on Windows.

The GUI provides the same features as the CLI: search, lookup, download, and OCR batch -- with a progress bar and live log output. When Surya OCR is selected, the GUI automatically routes the work through the memory-protection harness.

## CLI Reference

```
usage: downloader.py [-h] [--lccn LCCN] [--source SOURCE] [--years YEARS]
                     [--output OUTPUT] [--search SEARCH] [--info INFO]
                     [--ocr {none,loc,surya,both}] [--max-issues MAX_ISSUES]
                     [--retry-failed] [--verbose] [--speed {safe,standard}]
                     [--ocr-batch] [--json]
```

| Flag | Description |
|---|---|
| `--lccn LCCN` | Newspaper LCCN identifier (e.g. `sn87080287`) |
| `--source SOURCE` | Archive source to use (default: `loc`) |
| `--search QUERY` | Search for newspapers by title |
| `--info LCCN` | Show details about a specific newspaper |
| `--years YEARS` | Filter by year range: `1900-1905`, `1900,1903`, or `1893,1895-1900` |
| `--output DIR` | Output directory (default: `downloads/<lccn>`) |
| `--ocr MODE` | OCR mode: `none` (default), `loc` (API), `surya` (local AI), `both` |
| `--ocr-batch` | Run OCR on already-downloaded files (requires prior download) |
| `--max-issues N` | Limit number of issues to process (`0` = all) |
| `--speed PROFILE` | Download speed: `safe` (15s delay, default) or `standard` (4s delay) |
| `--retry-failed` | Re-download previously failed pages |
| `--verbose` | Enable detailed debug logging |
| `--json` | Machine-readable JSON output (for `--search` and `--info`) |

## Rate Limiting

The downloader respects the Library of Congress API rate limits:

| Profile | Delay Between Downloads | Approx. Throughput |
|---|---|---|
| `safe` (default) | 15 seconds | ~4 requests/min |
| `standard` | 4 seconds | ~15 requests/min |

LOC enforces a burst limit of 20 requests per minute (5-minute block on violation) and a crawl limit of 20 requests per 10 seconds (1-hour block). The `safe` profile stays well under both limits. Use `standard` at your own risk for large batch jobs.

All requests include automatic retry with exponential backoff for transient errors (429, 5xx).

## Output Structure

Downloads are organized by year under the output directory:

```
downloads/sn87080287/
├── download_metadata.json        # Tracks all downloaded/failed issues
├── download.log                  # Session log
├── 1900/
│   ├── sn87080287_1900-01-04_ed-1_page01.pdf
│   ├── sn87080287_1900-01-04_ed-1_page02.pdf
│   ├── sn87080287_1900-01-04_ed-1_page01_loc.txt    # Tier 1 OCR
│   ├── sn87080287_1900-01-04_ed-1_page01_surya.txt  # Tier 2 OCR
│   └── ...
├── 1901/
│   └── ...
```

**File naming convention:** `{lccn}_{date}_ed-{edition}_page{NN}.pdf`

**OCR text files** include a header with metadata (LCCN, date, page number, OCR method) followed by the extracted text.

### Resume Behavior

The `download_metadata.json` file tracks every successfully downloaded issue. If you stop and restart, previously completed issues are automatically skipped. Use `--retry-failed` to re-attempt issues that partially failed.

## Memory-Protected AI OCR (Harness)

Surya AI OCR is memory-intensive. When you select `surya` or `both` OCR mode, the system can route the work through `harness.py` -- a process wrapper that monitors memory and CPU usage.

The harness will terminate the OCR process if:
- Memory usage exceeds **75% of available RAM** (configurable via `HARNESS_MEM_MB` env var)
- Runtime exceeds **120 minutes** (configurable via `HARNESS_TIMEOUT` env var)

To kill a running harness from another terminal:
```bash
python harness.py --kill
```

The GUI uses the harness automatically whenever Surya OCR is active.

## Windows Batch Launchers

| File | Description |
|---|---|
| `run.bat` | CLI launcher. Checks for Python and dependencies, then passes arguments to `downloader.py`. Run with no arguments for help. |
| `run_gui.bat` | GUI launcher. Double-click to open the graphical interface. Uses `pythonw` when available to hide the console window. |

Both scripts auto-install the `requests` library if it's missing.

## How It Works

The downloader uses a **Source Abstraction Layer** to decouple download logic from archive-specific APIs:

1. **Selection** -- The `DownloadManager` initializes a source engine (e.g. `LOCSource`) based on `--source`.
2. **Discovery** -- The source queries the archive API with year filtering to build an issue list.
3. **Procurement** -- Pages are fetched as high-quality, sequentially-named PDFs organized by year.
4. **Enrichment** -- OCR text is extracted using the selected tier. Surya OCR runs in a monitored process harness to protect system memory.
5. **Tracking** -- Each completed issue is recorded in `download_metadata.json` for resume support.

## Developer Guide

Want to add a new archive source or contribute to the OCR pipeline? See [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md).

## License & Credits

[MIT](LICENSE). This tool is built for educational and historical research.

Newspapers in the Chronicling America collection are provided by the **Library of Congress** and the **National Endowment for the Humanities** and are generally in the public domain.
