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

## Quick Start

### 1. Install Dependencies

Standard mode (LOC download only):
```bash
pip install requests
```

AI OCR mode (Surya-powered):
```bash
pip install surya-ocr pymupdf torch
```

*Or just use `run.bat` / `run_gui.bat` \u2013 they handle setup for you.*

### 2. Search & Info

```bash
# Search across archives (default: loc)
python downloader.py --search "Freeland Tribune"

# Get details about a specific newspaper
python downloader.py --info sn87080287
```

### 3. Download & OCR

```bash
# Basic download
python downloader.py --lccn sn87080287

# Download + Tier 1 (Source API) OCR
python downloader.py --lccn sn87080287 --ocr loc

# Download + Tier 2 (AI-powered Surya) OCR
python downloader.py --lccn sn87080287 --ocr surya

# Retroactive OCR on existing downloads
python downloader.py --lccn sn87080287 --ocr both --ocr-batch
```

## CLI Reference

```
usage: downloader.py [-h] [--lccn LCCN] [--source SOURCE] [--years YEARS]
                     [--output OUTPUT] [--search SEARCH] [--info INFO]
                     [--ocr {none,loc,surya,both}] [--max-issues MAX_ISSUES]
                     [--retry-failed] [--verbose] [--speed {safe,standard}]
                     [--ocr-batch] [--json]

  --source SOURCE       Archive source (default: loc)
  --years YEARS         Year range (e.g. "1900-1905" or "1900,1903")
  --ocr {none,loc,surya,both}
                        OCR mode: 
                          loc:    Fast, fetch from Source API
                          surya:  AI-powered layout analysis (local)
                          both:   Attempt both tiers
  --ocr-batch           Run OCR only (requires existing downloads)
  --json                Machine-readable output for scripts
```

## How It Works

The downloader uses a **Source Abstraction Layer**:
1.  **Selection**: The `DownloadManager` initializes a source engine (e.g., `LOCSource`).
2.  **Discovery**: High-performance filtered queries discover issue lists.
3.  **Procurement**: Pages are fetched as high-quality sequence-named PDFs.
4.  **Enrichment**: OCR is processed based on the selected Tier. Surya OCR runs in a monitored process harness to protect system memory.

## Developer Guide

Want to add a new archive or contribute to the OCR pipeline? See [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md).

## License & Credits

[MIT](LICENSE). This tool is built for educational and historical research. 

Newspapers in the Chronicling America collection are provided by the **Library of Congress** and the **National Endowment for the Humanities** and are generally in the public domain.
