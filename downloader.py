#!/usr/bin/env python3
"""
PaperRouter
A robust, extensible tool to download and OCR historical newspaper
editions from various archives.

Supports any newspaper available in the collection by LCCN identifier.

Requirements: Python 3.7+
Install dependencies: pip install requests
"""

import os
import re
import sys
import json
import time
import shutil
import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union

from ocr_engine import OCRManager
from sources import get_source, NewspaperSource, IssueMetadata, PageMetadata, TitleResult

try:
    import requests
    from requests.adapters import HTTPAdapter
    from requests.packages.urllib3.util.retry import Retry
except ImportError:
    print("ERROR: The 'requests' library is required.")
    print("Please install it by running: pip install requests")
    sys.exit(1)


# Configuration
DOWNLOAD_DIR = "downloads"
METADATA_FILE = "download_metadata.json"

# LOC actual rate limits (from API docs):
#   Burst Limit: 20 requests per 1 minute -> blocked 5 minutes
#   Crawl Limit: 20 requests per 10 seconds -> blocked 1 hour
#
# Speed profiles (user-selectable via --speed):
#   "safe"     = 15 s between downloads  (~4 req/min)   - default
#   "standard" = 4 s between downloads   (~15 req/min)  - within burst limit
SPEED_PROFILES = {
    'safe':     {'download': 15.0, 'scan': 3.0},
    'standard': {'download': 4.0,  'scan': 2.0},
}
DEFAULT_SPEED = 'safe'
MAX_RETRIES = 5
RETRY_BACKOFF = 2  # Exponential backoff multiplier
CHUNK_SIZE = 8192  # For streaming downloads
API_PAGE_SIZE = 100  # Items per page when querying the collection API

# LCCN format: typically "sn" followed by 8 digits, but other prefixes exist
LCCN_PATTERN = re.compile(r'^[a-z]{1,3}\d{8,10}$')


# ---------------------------------------------------------------------------
# Newspaper lookup / search helpers
# ---------------------------------------------------------------------------

def validate_lccn(lccn: str) -> bool:
    """Check whether a string looks like a valid LCCN identifier."""
    return bool(LCCN_PATTERN.match(lccn))


def create_session() -> requests.Session:
    """Create a requests session with automatic retry logic."""
    session = requests.Session()

    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        'User-Agent': 'PaperRouter/2.0 (educational research)'
    })
    return session


# Helper: Search Newspapers (Source-Agnostic)
def search_newspapers(query: str, source_name: str = 'loc') -> List[TitleResult]:
    """Search for newspapers using the specified source."""
    source = get_source(source_name)
    return source.search_titles(query)

def get_newspaper_info(lccn: str, source: NewspaperSource) -> Optional[Dict]:
    """Fetch metadata about a newspaper by its LCCN using the source abstraction."""
    issues = source.fetch_issues(lccn)
    if not issues:
        return None

    title = issues[0].title if issues[0].title else "Unknown"

    start_year = min(i.year for i in issues) if issues else None
    end_year = max(i.year for i in issues) if issues else None

    return {
        'lccn': lccn,
        'title': title,
        'place': 'Unknown',
        'start_year': start_year,
        'end_year': end_year,
        'url': issues[0].url if issues else "",
    }


# ---------------------------------------------------------------------------
# Download manager
# ---------------------------------------------------------------------------

class DownloadManager:
    """Manages the download process with pluggable archive sources."""

    def __init__(self, lccn: str, output_dir: str,
                 source_name: str = 'loc',
                 years: Optional[List[int]] = None,
                 verbose: bool = False, retry_failed: bool = False,
                 speed: str = DEFAULT_SPEED, ocr_mode: str = 'none',
                 max_issues: int = 0):
        self.lccn = lccn
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self._setup_logging(verbose)
        self.source = get_source(source_name, logger=self.logger)

        self.metadata_path = self.output_dir / METADATA_FILE
        self.metadata = self._load_metadata()

        self.years = years
        self.year_set = set(years) if years else None
        self.verbose = verbose
        self.retry_failed = retry_failed

        profile = SPEED_PROFILES.get(speed, SPEED_PROFILES[DEFAULT_SPEED])
        self.download_delay = profile['download']
        self.scan_delay = profile['scan']
        self.speed_name = speed

        self.session = create_session()
        self.ocr_mode = ocr_mode
        self.max_issues = max_issues
        self.ocr_manager = OCRManager(self.output_dir, self.logger)

        self.newspaper_title = self.metadata.get('newspaper_title', lccn)

        self.stats = {
            'downloaded': 0,
            'skipped': 0,
            'failed': 0,
            'total_bytes': 0,
        }

    def _setup_logging(self, verbose: bool):
        self.logger = logging.getLogger(f'Downloader.{self.lccn}')
        self.logger.setLevel(logging.DEBUG if verbose else logging.INFO)
        self.logger.handlers.clear()

        log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

        file_handler = logging.FileHandler(self.output_dir / 'download.log')
        file_handler.setFormatter(log_format)
        self.logger.addHandler(file_handler)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(log_format)
        self.logger.addHandler(console_handler)

    def _load_metadata(self) -> Dict:
        if self.metadata_path.exists():
            try:
                with open(self.metadata_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                self.logger.warning(f"Could not load metadata file: {e}")
        return {'downloaded': {}, 'failed': {}, 'lccn': self.lccn}

    def _save_metadata(self):
        try:
            self.metadata['lccn'] = self.lccn
            self.metadata['newspaper_title'] = self.newspaper_title
            with open(self.metadata_path, 'w') as f:
                json.dump(self.metadata, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save metadata: {e}")

    def _rate_limit(self, scan=False):
        time.sleep(self.scan_delay if scan else self.download_delay)

    def _fetch_newspaper_issues(self) -> List[IssueMetadata]:
        issues = self.source.fetch_issues(self.lccn, year_set=self.year_set)
        if issues:
            self.newspaper_title = issues[0].title
            self._save_metadata()
        return issues

    def run(self):
        """Execute the download process."""
        self.logger.info("=" * 70)
        self.logger.info(f"PaperRouter (Source: {self.source.display_name})")
        self.logger.info(f"Newspaper: {self.newspaper_title} (LCCN: {self.lccn})")
        self.logger.info(f"Output: {self.output_dir.absolute()}")
        self.logger.info("-" * 40)

        issues = self._fetch_newspaper_issues()
        if not issues:
            self.logger.warning("No issues found matching criteria.")
            return

        if self.max_issues > 0:
            issues = issues[:self.max_issues]

        self.logger.info(f"Will process {len(issues)} issues...")

        start_time = time.time()
        for i, issue in enumerate(issues, 1):
            issue_id = f"{issue.date}_ed-{issue.edition}"
            self.logger.info(f"\n[{i}/{len(issues)}] Processing {issue_id}")

            if issue_id in self.metadata['downloaded'] and not self.retry_failed:
                if self.ocr_mode == 'none':
                    self.logger.info(f"  Skipping (already downloaded)")
                    self.stats['skipped'] += 1
                    continue

            pages = self.source.get_pages_for_issue(issue)
            if not pages:
                self.logger.error(f"  No pages found.")
                self.stats['failed'] += 1
                continue

            success_count = 0
            downloaded_pages_meta = []
            for page in pages:
                filename = f"{issue.lccn}_{page.issue_date}_ed-{page.edition}_page{page.page_num:02d}.pdf"
                year_dir = self.output_dir / str(issue.year)
                dest_path = year_dir / filename

                download_ok = True
                if not dest_path.exists() or self.retry_failed:
                    res = self.source.download_page_pdf(page, dest_path)
                    if res.success:
                        self.stats['downloaded'] += 1
                        self.stats['total_bytes'] += res.size_bytes
                        self._rate_limit()
                    else:
                        self.logger.error(f"  Failed page {page.page_num}: {res.error}")
                        download_ok = False
                
                if download_ok:
                    success_count += 1
                    downloaded_pages_meta.append({
                        'page': page.page_num,
                        'file': str(dest_path.relative_to(self.output_dir)),
                        'size': dest_path.stat().st_size if dest_path.exists() else 0
                    })
                    
                    if self.ocr_mode != 'none':
                        self.ocr_manager.process_page(page, self.source, self.ocr_mode, pdf_path=dest_path)

            if success_count == len(pages):
                self.metadata['downloaded'][issue_id] = {
                    'date': issue.date,
                    'edition': issue.edition,
                    'complete': True,
                    'downloaded_at': datetime.now().isoformat(),
                    'pages': downloaded_pages_meta
                }
                if issue_id in self.metadata['failed']:
                    del self.metadata['failed'][issue_id]
            else:
                self.metadata['failed'][issue_id] = f"Partial: {success_count}/{len(pages)}"

            self._save_metadata()

        elapsed = time.time() - start_time
        self.logger.info("\n" + "=" * 70)
        self.logger.info("PROCESS COMPLETE")
        self.logger.info(f"Pages downloaded: {self.stats['downloaded']}")
        self.logger.info(f"Issues skipped: {self.stats['skipped']}")
        self.logger.info(f"Issues failed: {self.stats['failed']}")
        self.logger.info(f"Time: {elapsed / 60:.1f} minutes")
        self.logger.info("=" * 70)

    def run_ocr_batch(self):
        """Run OCR on all previously downloaded issues."""
        self.logger.info("=" * 70)
        self.logger.info(f"STARTING OCR BATCH: {self.newspaper_title}")
        self.logger.info(f"LCCN: {self.lccn} | Mode: {self.ocr_mode}")
        self.logger.info("=" * 70)

        issues_count = 0
        pages_count = 0

        downloaded = self.metadata.get('downloaded', {})
        if not downloaded:
            self.logger.warning("No downloaded issues found in metadata!")
            return

        for issue_id, info in downloaded.items():
            issue_pages = info.get('pages', [])
            if not issue_pages:
                continue

            issues_count += 1
            self.logger.info(f"Processing issue {issue_id} ({len(issue_pages)} pages)...")

            for page_info in issue_pages:
                page_num = page_info['page']
                page_file = page_info['file']
                pdf_path = self.output_dir / page_file

                # Reconstruct a PageMetadata for the OCR manager
                page_meta = PageMetadata(
                    issue_date=info['date'],
                    edition=info['edition'],
                    page_num=page_num,
                    url=self.source.build_page_url(self.lccn, info['date'], info['edition'], page_num),
                    lccn=self.lccn
                )

                self.ocr_manager.process_page(page_meta, self.source, self.ocr_mode, pdf_path=pdf_path)
                pages_count += 1

        self.logger.info("\n" + "=" * 70)
        self.logger.info(f"OCR batch complete.")
        self.logger.info(f"Processed {pages_count} pages across {issues_count} issues.")
        self.logger.info("=" * 70)


# ---------------------------------------------------------------------------
# Helper: Parse Year Range
def parse_year_range(year_str: str) -> List[int]:
    """Parse a string like '1893,1895-1900' into a list of years."""
    years = set()
    for part in year_str.split(','):
        part = part.strip()
        if '-' in part:
            start, end = part.split('-')
            years.update(range(int(start), int(end) + 1))
        else:
            years.add(int(part))
    return sorted(list(years))


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="PaperRouter: Multi-Source Newspaper Downloader")
    parser.add_argument("--lccn", help="Newspaper LCCN (e.g. sn87080287)")
    parser.add_argument("--source", default="loc", help="Archive source (default: loc)")
    parser.add_argument("--years", help="Years to download (e.g. 1893,1895-1900)")
    parser.add_argument("--output", help="Output directory")
    parser.add_argument("--search", help="Search for newspapers by title")
    parser.add_argument("--info", help="Get information about a specific LCCN")
    parser.add_argument("--ocr", choices=['none', 'loc', 'surya', 'both'], default='none',
                        help="OCR mode (default: none)")
    parser.add_argument("--max-issues", type=int, default=0, help="Limit number of issues (0=all)")
    parser.add_argument("--retry-failed", action="store_true", help="Retry previously failed pages")
    parser.add_argument("--verbose", action="store_true", help="Detailed logging")
    parser.add_argument("--speed", choices=["safe", "standard"], default="safe", help="Download speed profile")
    parser.add_argument("--ocr-batch", action="store_true", help="Run OCR on already-downloaded files")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    args = parser.parse_args()

    if args.search:
        results = search_newspapers(args.search, source_name=args.source)
        if args.json:
            import dataclasses
            print(json.dumps([dataclasses.asdict(r) for r in results], ensure_ascii=False))
        else:
            if not results:
                print("No newspapers found.")
            else:
                print(f"\nSearch results for '{args.search}' ({args.source}):")
                for r in results:
                    print(f"  {r.lccn}: {r.title} ({r.place}, {r.dates})")
        return

    # Info can use --lccn or be passed its own value
    target_lccn = args.lccn or args.info
    source = get_source(args.source)

    if args.info:
        if not target_lccn:
            print("Error: Please provide an LCCN via --info or --lccn")
            sys.exit(1)
        info = get_newspaper_info(target_lccn, source)
        if info:
            if args.json:
                print(json.dumps(info, ensure_ascii=False))
            else:
                print(f"\nNewspaper: {info['title']}")
                print(f"LCCN:      {info['lccn']}")
                print(f"Range:     {info['start_year'] or '?'}-{info['end_year'] or '?'}")
                print(f"URL:       {info['url']}")
        else:
            if args.json:
                print("{}")
            else:
                print(f"Could not find information for LCCN: {target_lccn}")
        return

    if not args.lccn:
        parser.print_help()
        return

    # Parse years
    years = None
    if args.years:
        try:
            years = parse_year_range(args.years)
        except ValueError as e:
            print(f"Error parsing years: {e}")
            sys.exit(1)

    output = args.output or os.path.join(DOWNLOAD_DIR, args.lccn)

    manager = DownloadManager(
        lccn=args.lccn,
        source_name=args.source,
        output_dir=output,
        years=years,
        verbose=args.verbose,
        retry_failed=args.retry_failed,
        speed=args.speed,
        ocr_mode=args.ocr,
        max_issues=args.max_issues
    )

    if args.ocr_batch:
        manager.run_ocr_batch()
    else:
        manager.run()


if __name__ == '__main__':
    main()
