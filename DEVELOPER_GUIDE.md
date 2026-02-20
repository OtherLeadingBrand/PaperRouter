# Developer Guide

This guide describes the architecture of **PaperRouter** and explains how to extend it with new sources or OCR engines.

## Architecture Overview

The system is built around a **Pluggable Source Architecture**. This decouples the download logic from specific archive APIs, allowing the platform to grow without modifying the core `DownloadManager`.

### Directory Structure

```text
/
├── downloader.py          # CLI entry point and DownloadManager orchestration
├── web_gui.py             # Flask-based web interface (primary GUI, dark-themed)
├── gui.py                 # Legacy Tkinter-based interface (deprecated)
├── ocr_engine.py          # Tier 1 & 2 OCR management (SuryaOCREngine, OCRManager)
├── harness.py             # Resource-monitored process wrapper for AI workers
├── run.bat                # Windows CLI launcher
├── run_gui.bat            # Windows Web GUI launcher (auto-installs deps)
├── requirements.txt       # Python dependencies (requests, flask, psutil)
├── VERSION                # SemVer version string (e.g. 0.1.0-alpha)
├── sources/
│   ├── __init__.py        # Source registry & get_source() factory
│   ├── base.py            # Abstract base classes and dataclass schemas
│   └── loc_source.py      # Library of Congress (Chronicling America) implementation
├── docs/
│   └── screenshots/       # README screenshots (auto-generated)
├── tests/
│   ├── debug_loc.py       # LOC API debugging utilities
│   ├── check_surya_imports.py
│   └── list_surya.py
```

### Component Relationships

```
CLI (downloader.py)          Web GUI (web_gui.py)
        │                        │
        ▼                        ▼ (subprocess)
   DownloadManager ─────► harness.py (when Surya active)
        │                        │
        ├── Source (LOCSource)   ▼
        │      fetch_issues()    downloader.py (child process)
        │      get_pages_for_issue()
        │      download_page_pdf()
        │      fetch_ocr_text()
        │
        └── OCRManager
               ├── Source.fetch_ocr_text()   [Tier 1]
               └── SuryaOCREngine            [Tier 2]
```

The Web GUI does not import `DownloadManager` directly. It spawns `downloader.py` (or `harness.py`) as a subprocess and parses its stdout to stream progress events to the browser via Server-Sent Events (SSE). The web server uses dynamic port selection, trying ports 5000, 5001, 8080, and others in sequence.

---

## Data Model

All shared data types are defined as dataclasses in `sources/base.py`:

### IssueMetadata

Represents a single newspaper issue (one day's edition).

| Field | Type | Description |
|---|---|---|
| `date` | `str` | ISO date, e.g. `"1900-01-04"` |
| `edition` | `int` | Edition number (usually `1`) |
| `url` | `str` | API URL for this issue |
| `year` | `int` | Extracted year (for filtering/sorting) |
| `lccn` | `str` | Parent newspaper LCCN |
| `title` | `str` | Newspaper title |
| `pages` | `List[PageMetadata]` | Populated after `get_pages_for_issue()` |

### PageMetadata

Represents a single page within an issue.

| Field | Type | Description |
|---|---|---|
| `issue_date` | `str` | ISO date of the parent issue |
| `edition` | `int` | Edition number |
| `page_num` | `int` | 1-indexed page number |
| `url` | `str` | Page item URL (used for metadata/OCR lookups) |
| `pdf_url` | `str` | Direct PDF download URL (may be empty until resolved) |
| `ocr_url` | `str` | External OCR endpoint URL |
| `lccn` | `str` | Parent newspaper LCCN |

### Result Types

- **`DownloadResult`** -- `success`, `path`, `error`, `size_bytes`
- **`OCRResult`** -- `success`, `text_path`, `word_count`, `error`
- **`TitleResult`** -- `lccn`, `title`, `place`, `dates`, `url`

---

## Adding a New Source

To add a new newspaper archive (e.g. Trove, British Newspaper Archive), follow these steps:

### 1. Create the Source Class

Create `sources/my_source.py` and subclass `NewspaperSource`:

```python
from .base import (
    NewspaperSource, IssueMetadata, PageMetadata,
    DownloadResult, OCRResult, TitleResult,
)

class MySource(NewspaperSource):

    @property
    def name(self) -> str:
        return "mysource"

    @property
    def display_name(self) -> str:
        return "My Newspaper Archive"

    def search_titles(self, query: str) -> List[TitleResult]:
        # Search the archive for newspapers matching the query.
        # Return a list of TitleResult with at minimum lccn and title.
        ...

    def fetch_issues(self, lccn: str, year_set=None) -> List[IssueMetadata]:
        # Discover all issues for a newspaper.
        # If year_set is provided, filter at the API level for performance.
        # Return sorted by (date, edition).
        ...

    def get_pages_for_issue(self, issue: IssueMetadata) -> List[PageMetadata]:
        # Given an issue, return metadata for each page.
        # page_num should be 1-indexed.
        ...

    def download_page_pdf(self, page: PageMetadata, dest_path: Path) -> DownloadResult:
        # Download the PDF for a page to dest_path.
        # Create parent directories as needed.
        ...

    def fetch_ocr_text(self, page: PageMetadata, output_dir: Path) -> OCRResult:
        # Optional: Fetch archive-provided OCR text (Tier 1).
        # Save to output_dir with filename:
        #   {date}_ed-{edition}_page{NN}_{source}.txt
        # Return OCRResult(success=False) if the archive has no OCR.
        ...

    def build_page_url(self, lccn, date, edition, page_num) -> str:
        # Reconstruct a page URL from components.
        # Used by OCR batch mode to rebuild PageMetadata from saved metadata.
        ...
```

### 2. Register the Source

Add your source to `sources/__init__.py`:

```python
from .loc_source import LOCSource
from .my_source import MySource

SOURCES = {
    'loc': LOCSource,
    'mysource': MySource,
}
```

That's it. The CLI `--source mysource` and the Web GUI source dropdown will pick it up automatically.

### 3. Implementation Notes

- **Year filtering**: If the archive API supports date filtering, use it in `fetch_issues()` when `year_set` is provided. This is critical for performance -- the LOC archive has millions of pages, and filtering at the API level reduces pagination from hundreds of requests to a handful.
- **Rate limiting**: Each source manages its own HTTP session and rate limiting. Don't rely on the `DownloadManager` for this.
- **Error handling**: Return `DownloadResult(success=False, error="...")` rather than raising exceptions. The `DownloadManager` logs errors and tracks them in metadata for retry.
- **Page numbering**: Pages must be 1-indexed. The file naming convention (`page01.pdf`, `page02.pdf`) depends on this.

---

## OCR System

The OCR system uses two tiers, managed by `OCRManager` in `ocr_engine.py`:

### Tier 1: Source-Provided OCR

Each `NewspaperSource` can implement `fetch_ocr_text()` to retrieve pre-processed OCR from the archive's API. For LOC, this fetches text from the word-coordinates service and applies post-processing (artifact filtering, heading detection, hyphen rejoining).

Output filename pattern: `{date}_ed-{edition}_page{NN}_loc.txt`

### Tier 2: Surya AI OCR

`SuryaOCREngine` uses the `surya-ocr` library to perform local layout analysis and text recognition on downloaded PDFs. The pipeline:

1. Convert PDF page to image via PyMuPDF (1.5x zoom for quality)
2. Run Surya layout prediction to detect text regions
3. Run Surya OCR recognition with bounding boxes
4. Extract text lines and write to file

Output filename pattern: `{date}_ed-{edition}_page{NN}_surya.txt`

Models are lazy-loaded on first use. The `FoundationPredictor` is shared between the detection, recognition, and layout predictors.

### The Process Harness

Because Surya loads large ML models into memory, Tier 2 OCR can easily consume many gigabytes of RAM. The `harness.py` wrapper provides safety:

- Spawns `downloader.py` as a child process in its own process group
- Polls the process tree every 10 seconds for memory and CPU usage
- Kills the entire tree if RSS exceeds **75% of available RAM** (or `HARNESS_MEM_MB`)
- Kills on timeout after **120 minutes** (or `HARNESS_TIMEOUT`)
- Writes a PID file (`.harness.pid`) for external kill support (`python harness.py --kill`)

The Web GUI routes through the harness automatically when Surya is active.

---

## LOC API Specifics

Key details for anyone working on the `LOCSource` implementation:

### Collection API

- **Base URL**: `https://www.loc.gov/collections/chronicling-america/`
- **Filter by LCCN**: `?fa=number_lccn:{lccn}&fo=json`
- **Filter by year**: Append `&dates=YYYY` or `&dates=YYYY/YYYY`
- **Pagination**: Results include `pagination.next` URL; follow until `null`
- **Page size**: Controlled by `&c=100` (max 100 items per page)

### Issue Detail API

- Append `?fo=json` to an issue URL to get JSON metadata
- The response contains `resources[0].files[]` -- each element is a **page**
- Each page is a list of file variants (PDF, JP2, XML) with different `mimetype` values
- Per-page URLs use `?sp=N` suffix (e.g. `?sp=2` for page 2)

### Search Results Quirks

- **LCCN field**: Search results use `number_lccn` (a list), not `lccn`
- **Title field**: `partof_title` (a list) contains the newspaper name; `title` is a per-page description
- **Location**: Combine `location_city` and `location_state` (both lists)

### OCR API

- Page JSON exposes `resource.fulltext_file` or `fulltext_service`
- The word-coordinates service returns JSON keyed by a segment ID
- Actual text is in `segment_id.full_text`
- Append `?full_text=1` to the word-coordinates URL if not already present

---

## Metadata & Resume System

The `DownloadManager` maintains a JSON metadata file (`download_metadata.json`) in the output directory:

```json
{
  "lccn": "sn87080287",
  "newspaper_title": "Freeland tribune.",
  "downloaded": {
    "1900-01-04_ed-1": {
      "date": "1900-01-04",
      "edition": 1,
      "complete": true,
      "downloaded_at": "2026-02-18T14:30:00",
      "pages": [
        {"page": 1, "file": "1900/sn87080287_1900-01-04_ed-1_page01.pdf", "size": 245000},
        {"page": 2, "file": "1900/sn87080287_1900-01-04_ed-1_page02.pdf", "size": 312000}
      ]
    }
  },
  "failed": {
    "1900-01-11_ed-1": "Partial: 3/4"
  }
}
```

- Issues in `downloaded` with `complete: true` are skipped on subsequent runs
- Issues in `failed` are logged but not retried unless `--retry-failed` is passed
- The `pages` array is used by `--ocr-batch` to reconstruct `PageMetadata` objects for retroactive OCR

---

## Testing a New Source

Verify the full pipeline:

1. **Search**: `python downloader.py --source mysource --search "Query"`
2. **Info**: `python downloader.py --source mysource --info "ID"`
3. **Download (small)**: `python downloader.py --source mysource --lccn "ID" --max-issues 1`
4. **Tier 1 OCR**: Check that `_mysource.txt` files appear in the year directory
5. **Tier 2 OCR**: `python downloader.py --source mysource --lccn "ID" --ocr surya --max-issues 1`
6. **OCR batch**: Download first, then `--ocr-batch` -- verify `build_page_url()` reconstructs correct URLs
7. **Resume**: Run the same download command twice; second run should skip all issues
