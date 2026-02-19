# Developer Guide

This guide describes the architecture of the Multi-Source Newspaper Downloader and provides instructions on how to extend it.

## Architecture Overview

The system is built around a **Pluggable Source Architecture**. This decouple the download logic from the specific archive APIs, allowing the platform to grow without modifying the core `DownloadManager`.

### Directory Structure

```text
/
├── downloader.py          # Main entry point and orchestration
├── ocr_engine.py          # Tier 1 & 2 OCR management
├── harness.py              # Resource-monitored process wrapper for AI workers
├── gui.py                  # Tkinter-based user interface
├── sources/
│   ├── __init__.py         # Source registry & factory
│   ├── base.py              # Abstract base classes and metadata schemas
│   └── loc_source.py       # Library of Congress implementation
├── tests/                  # Unit and integration tests
```

---

## Adding a New Source

To add a new archive source (e.g., `NewYorkTimesSource`), follow these steps:

### 1. Define the Source in `sources/`

Create a new file `sources/nyt_source.py` and inherit from `NewspaperSource`:

```python
from .base import NewspaperSource, IssueMetadata, PageMetadata, TitleResult

class NYTSource(NewspaperSource):
    def search_titles(self, query: str) -> List[TitleResult]:
        # Implement title search logic
        pass

    def fetch_issues(self, lccn: str, years: Optional[List[int]] = None) -> List[IssueMetadata]:
        # Implement issue discovery (use years for optimization!)
        pass

    def fetch_page_metadata(self, issue: IssueMetadata) -> List[PageMetadata]:
        # Fetch per-page URLs and metadata
        pass

    def download_page_pdf(self, page: PageMetadata, dest_path: Path) -> DownloadResult:
        # Implementation-specific download logic
        pass

    def fetch_ocr_text(self, page: PageMetadata, output_dir: Path) -> OCRResult:
        # Optional: Tier 1 OCR fetching from API
        pass
```

### 2. Register the Source

Add your source to `sources/__init__.py`:

```python
from .loc_source import LOCSource
from .nyt_source import NYTSource  # New!

SOURCES = {
    'loc': LOCSource,
    'nyt': NYTSource,  # Register!
}
```

---

## OCR System

The system uses a tiered approach managed by `OCRManager`:

-   **Tier 1 (Source)**: The `NewspaperSource` implementation can optionally fetch pre-processed OCR text provided by the archive's API (e.g., LOC Alto XML).
-   **Tier 2 (AI/Surya)**: A shared engine that uses the `surya-ocr` model to perform layout analysis and recognition on downloaded PDFs.

### The Harness Logic

Because AI OCR is memory-intensive, Tier 2 processing is often routed through `harness.py`. The harness monitors the subprocess tree and kills it if it exceeds a memory ceiling (default: 75% of available RAM), preventing system crashes.

---

## Testing

When adding a new source, verify the following:

1.  **Search**: `python downloader.py --source MY_SOURCE --search "Query"`
2.  **Info**: `python downloader.py --source MY_SOURCE --info "ID"`
3.  **Download**: `python downloader.py --source MY_SOURCE --lccn "ID" --max-issues 1`
4.  **OCR**: Verify both Tier 1 and Tier 2 outputs in the year-based directories.

---

## Metadata Schema

The system relies on dataclasses defined in `sources/base.py`:

-   `IssueMetadata`: Represents a specific newspaper issue (date, edition, title).
-   `PageMetadata`: Represents a single page within an issue (page number, PDF URL, metadata URL).
-   `DownloadResult` / `OCRResult`: Standard responses for operation success/failure.
