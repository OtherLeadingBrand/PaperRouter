#!/usr/bin/env python3
"""
LOC Newspaper Downloader
Downloads historical newspaper editions from the Library of Congress
Chronicling America collection.

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
from typing import Dict, List, Optional

try:
    import requests
    from requests.adapters import HTTPAdapter
    from requests.packages.urllib3.util.retry import Retry
except ImportError:
    print("ERROR: The 'requests' library is required.")
    print("Please install it by running: pip install requests")
    sys.exit(1)


# Configuration
BASE_URL = "https://www.loc.gov"
COLLECTION_API_URL = f"{BASE_URL}/collections/chronicling-america/"
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
        'User-Agent': 'LOCNewspaperDownloader/2.0 (educational research)'
    })
    return session


def search_newspapers(query: str) -> List[Dict]:
    """Search Chronicling America for newspapers matching a query string.

    Returns a list of dicts with keys: lccn, title, place, dates, url.
    """
    session = create_session()

    results = []
    api_url = (
        f"{COLLECTION_API_URL}"
        f"?q={requests.utils.quote(query)}"
        f"&c=50&fo=json"
    )

    try:
        response = session.get(api_url, timeout=30)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"Error searching: {e}")
        return []

    seen_lccns = set()
    for item in data.get('results', []):
        # Title-level records have short date strings (just a year or range)
        # and a number_lccn field
        lccn = item.get('number_lccn', [''])[0] if isinstance(item.get('number_lccn'), list) else item.get('number_lccn', '')
        if not lccn or lccn in seen_lccns:
            continue
        seen_lccns.add(lccn)

        title = item.get('title', 'Unknown')
        # Clean up title (often has trailing period or location in parens)
        title = title.strip().rstrip('.')

        place_parts = []
        for field in ('location_city', 'location_state'):
            vals = item.get(field, [])
            if isinstance(vals, list):
                place_parts.extend(vals)
            elif vals:
                place_parts.append(str(vals))
        place = ', '.join(place_parts) if place_parts else 'Unknown'

        dates = item.get('date', '')

        results.append({
            'lccn': lccn,
            'title': title,
            'place': place,
            'dates': dates,
            'url': item.get('url', ''),
        })

    return results


def get_newspaper_info(lccn: str) -> Optional[Dict]:
    """Fetch metadata about a newspaper by its LCCN.

    Returns dict with keys: lccn, title, place, start_year, end_year, url,
    or None if not found.
    """
    session = create_session()

    # Query the collection API filtered by LCCN
    api_url = (
        f"{COLLECTION_API_URL}"
        f"?fa=number_lccn:{lccn}"
        f"&c=1&fo=json"
    )

    try:
        response = session.get(api_url, timeout=30)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"Error fetching newspaper info: {e}")
        return None

    results = data.get('results', [])
    if not results:
        return None

    # Find the title-level record (short date like "1888" vs "1888-01-15")
    title_record = None
    any_record = None
    for item in results:
        any_record = item
        item_date = item.get('date', '')
        if len(item_date) <= 10 and '-' not in item_date[5:]:
            # This is likely a title record with just a year
            title_record = item
            break

    record = title_record or any_record
    if not record:
        return None

    title = record.get('title', 'Unknown').strip().rstrip('.')

    place_parts = []
    for field in ('location_city', 'location_state'):
        vals = record.get(field, [])
        if isinstance(vals, list):
            place_parts.extend(vals)
        elif vals:
            place_parts.append(str(vals))
    place = ', '.join(place_parts) if place_parts else 'Unknown'

    # Detect year range by scanning all items
    # We'll need to paginate to find the full date range
    start_year = None
    end_year = None

    scan_url = (
        f"{COLLECTION_API_URL}"
        f"?fa=number_lccn:{lccn}"
        f"&c={API_PAGE_SIZE}&fo=json"
    )

    while scan_url:
        try:
            resp = session.get(scan_url, timeout=30)
            resp.raise_for_status()
            page_data = resp.json()
        except Exception:
            break

        for item in page_data.get('results', []):
            item_date = item.get('date', '')
            if len(item_date) >= 4:
                try:
                    year = int(item_date[:4])
                    if start_year is None or year < start_year:
                        start_year = year
                    if end_year is None or year > end_year:
                        end_year = year
                except ValueError:
                    pass

        pagination = page_data.get('pagination', {})
        scan_url = pagination.get('next') if isinstance(pagination, dict) else None
        if scan_url:
            time.sleep(SPEED_PROFILES[DEFAULT_SPEED]['scan'])

    return {
        'lccn': lccn,
        'title': title,
        'place': place,
        'start_year': start_year,
        'end_year': end_year,
        'url': record.get('url', f'{BASE_URL}/item/{lccn}/'),
    }


# ---------------------------------------------------------------------------
# Download manager
# ---------------------------------------------------------------------------

class DownloadManager:
    """Manages the download process with resume capability and error handling."""

    def __init__(self, lccn: str, output_dir: str,
                 years: Optional[List[int]] = None,
                 verbose: bool = False, retry_failed: bool = False,
                 speed: str = DEFAULT_SPEED):
        self.lccn = lccn
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.metadata_path = self.output_dir / METADATA_FILE
        self.metadata = self._load_metadata()

        self.years = years
        self.year_set = set(years) if years else None  # None = all years
        self.verbose = verbose
        self.retry_failed = retry_failed

        profile = SPEED_PROFILES.get(speed, SPEED_PROFILES[DEFAULT_SPEED])
        self.download_delay = profile['download']
        self.scan_delay = profile['scan']
        self.speed_name = speed

        self._setup_logging(verbose)

        self.session = create_session()

        # Newspaper title (resolved during run)
        self.newspaper_title = self.metadata.get('newspaper_title', lccn)

        self.stats = {
            'downloaded': 0,
            'skipped': 0,
            'failed': 0,
            'total_bytes': 0,
        }

    def _setup_logging(self, verbose: bool):
        """Configure logging without accumulating duplicate handlers."""
        self.logger = logging.getLogger(f'LOCDownloader.{self.lccn}')
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
        """Load download metadata for resume capability."""
        if self.metadata_path.exists():
            try:
                with open(self.metadata_path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                backup_path = self.metadata_path.with_suffix('.json.bak')
                if backup_path.exists():
                    try:
                        with open(backup_path, 'r') as f:
                            print("Warning: Metadata corrupted, restored from backup.")
                            return json.load(f)
                    except Exception:
                        pass
                print(f"Warning: Could not load metadata file: {e}")
        return {'downloaded': {}, 'failed': {}, 'lccn': self.lccn}

    def _save_metadata(self):
        """Save download metadata atomically (write to temp, then rename)."""
        try:
            self.metadata['lccn'] = self.lccn
            self.metadata['newspaper_title'] = self.newspaper_title

            temp_path = self.metadata_path.with_suffix('.json.tmp')
            with open(temp_path, 'w') as f:
                json.dump(self.metadata, f, indent=2)

            if self.metadata_path.exists():
                backup_path = self.metadata_path.with_suffix('.json.bak')
                shutil.copy2(self.metadata_path, backup_path)

            shutil.move(str(temp_path), str(self.metadata_path))
        except Exception as e:
            self.logger.error(f"Failed to save metadata: {e}")

    def _is_file_complete(self, filepath: Path, expected_size: Optional[int] = None) -> bool:
        """Check if a downloaded file is complete and valid."""
        if not filepath.exists():
            return False

        file_size = filepath.stat().st_size
        if file_size < 1000:
            return False

        if expected_size and file_size != expected_size:
            return False

        try:
            with open(filepath, 'rb') as f:
                header = f.read(5)
                if header != b'%PDF-':
                    return False
        except Exception:
            return False

        return True

    def _rate_limit(self, scan: bool = False):
        """Apply rate limiting delay. Use scan=True for lighter API requests."""
        time.sleep(self.scan_delay if scan else self.download_delay)

    def _fetch_newspaper_issues(self) -> List[Dict]:
        """Fetch all available issues from the LOC collection API (paginated)."""
        self.logger.info(f"Fetching issue list for LCCN {self.lccn}...")
        self.logger.info("Querying Library of Congress collection API...")

        all_issues = []

        api_url = (
            f"{COLLECTION_API_URL}"
            f"?fa=number_lccn:{self.lccn}"
            f"&c={API_PAGE_SIZE}&fo=json"
        )

        page_num = 0
        while api_url:
            page_num += 1
            self.logger.info(f"  Fetching page {page_num} of issue list...")

            try:
                response = self.session.get(api_url, timeout=30)
                response.raise_for_status()
                data = response.json()
            except requests.exceptions.RequestException as e:
                self.logger.error(f"Failed to fetch issue list (page {page_num}): {e}")
                self._rate_limit(scan=True)
                continue
            except (json.JSONDecodeError, ValueError) as e:
                self.logger.error(f"Invalid JSON response on page {page_num}: {e}")
                self._rate_limit(scan=True)
                continue

            results = data.get('results', [])
            pagination = data.get('pagination', {})

            for item in results:
                item_date = item.get('date', '')
                if not item_date or len(item_date) < 8:
                    # Grab title from title-level records
                    title = item.get('title', '').strip().rstrip('.')
                    if title and self.newspaper_title == self.lccn:
                        self.newspaper_title = title
                    continue

                try:
                    year = int(item_date[:4])
                except (ValueError, IndexError):
                    continue

                if self.year_set and year not in self.year_set:
                    continue

                item_url = item.get('url', '') or item.get('id', '')
                if not item_url:
                    continue

                edition = 1
                if '/ed-' in item_url:
                    try:
                        ed_part = item_url.split('/ed-')[1].rstrip('/')
                        edition = int(ed_part)
                    except (ValueError, IndexError):
                        edition = 1

                all_issues.append({
                    'date': item_date,
                    'edition': edition,
                    'item_url': item_url,
                    'year': year,
                })

            next_url = pagination.get('next') if isinstance(pagination, dict) else None
            if next_url:
                api_url = next_url
                self._rate_limit(scan=True)
            else:
                api_url = None

        all_issues.sort(key=lambda x: x['date'])
        self.logger.info(f"Found {len(all_issues)} total issues")
        return all_issues

    def _get_pdf_urls_for_issue(self, item_url: str) -> List[Dict]:
        """Fetch issue JSON metadata and extract per-page PDF download URLs."""
        # Prefer the provided URL (usually /item/) as it often contains all pages
        # in one JSON response. Fall back to /resource/ if no PDFs are found.
        urls_to_try = [item_url]
        if '/item/' in item_url:
            urls_to_try.append(item_url.replace('/item/', '/resource/'))

        for base_url in urls_to_try:
            if not base_url.endswith('/'):
                base_url += '/'
            json_url = f"{base_url}?fo=json"

            try:
                self.logger.debug(f"Fetching issue JSON: {json_url}")
                response = self.session.get(json_url, timeout=30)
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                self.logger.error(f"Failed to fetch issue metadata from {json_url}: {e}")
                continue

        pdf_files = []
        for base_url in urls_to_try:
            if not base_url.endswith('/'):
                base_url += '/'
            json_url = f"{base_url}?fo=json"

            try:
                response = self.session.get(json_url, timeout=30)
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                self.logger.debug(f"Failed to fetch issue metadata from {json_url}: {e}")
                continue

            resources = data.get('resources', [])

            for resource_idx, resource in enumerate(resources, 1):
                # 1. Check for nested files list (common in /item/ JSON)
                file_groups = resource.get('files', [])
                for page_num, file_group in enumerate(file_groups, 1):
                    if not isinstance(file_group, list):
                        continue
                    for file_info in file_group:
                        if not isinstance(file_info, dict):
                            continue
                        if file_info.get('mimetype') == 'application/pdf':
                            url = file_info.get('url', '')
                            if url:
                                pdf_files.append({
                                    'url': url,
                                    'size': file_info.get('size'),
                                    'page': page_num,
                                })
                            break

                # 2. Check for direct 'pdf' key (common in /resource/ JSON)
                if not pdf_files:
                    pdf_url = resource.get('pdf')
                    if pdf_url:
                        pdf_files.append({
                            'url': pdf_url,
                            'size': resource.get('size'),
                            'page': 1,
                        })

            if pdf_files:
                break

        if not pdf_files and self.verbose:
            self.logger.debug(f"No PDFs found. Last base_url: {base_url}")

        return pdf_files

    def _download_file(self, url: str, output_path: Path,
                      expected_size: Optional[int] = None) -> bool:
        """Download a file with progress tracking and incomplete file detection."""
        temp_path = None

        if output_path.exists():
            if self._is_file_complete(output_path, expected_size):
                self.logger.debug(f"File already complete: {output_path.name}")
                return True
            else:
                self.logger.warning(f"Incomplete/invalid file, re-downloading: {output_path.name}")
                output_path.unlink()

        if not url.startswith('http'):
            url = BASE_URL + url

        try:
            response = self.session.get(url, stream=True, timeout=120)
            response.raise_for_status()

            content_type = response.headers.get('content-type', '')
            if 'text/html' in content_type:
                self.logger.warning(f"Got HTML instead of PDF for {output_path.name}")
                return False

            total_size = int(response.headers.get('content-length', 0))
            temp_path = output_path.with_suffix('.tmp')

            downloaded = 0
            last_progress = 0
            with open(temp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

                        if total_size > 0:
                            percent = int((downloaded / total_size) * 100)
                            if percent >= last_progress + 10:
                                last_progress = percent
                                self.logger.debug(
                                    f"  Progress: {percent}% "
                                    f"({downloaded // 1024}KB / {total_size // 1024}KB)"
                                )

            if downloaded == 0:
                self.logger.warning(f"Downloaded 0 bytes for {output_path.name}")
                if temp_path.exists():
                    temp_path.unlink()
                return False

            if expected_size and downloaded != expected_size:
                self.logger.warning(
                    f"Size mismatch for {output_path.name}: "
                    f"expected {expected_size}, got {downloaded}"
                )

            shutil.move(str(temp_path), str(output_path))
            self.stats['total_bytes'] += downloaded
            return True

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Download failed for {url}: {e}")
        except IOError as e:
            self.logger.error(f"File I/O error downloading {url}: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error downloading {url}: {e}")

        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return False

    def _is_download_verified(self, issue_id: str) -> bool:
        """Check if ALL pages of an issue are present and valid on disk.

        Returns True only when every page recorded in metadata still exists
        and passes PDF validation.  If any page is missing or corrupt the
        metadata entry is removed so the issue will be re-downloaded.
        """
        download_info = self.metadata.get('downloaded', {}).get(issue_id)
        if not download_info:
            return False

        # If metadata was written before the multi-page format, fall back to
        # checking the single 'file' key for backwards compatibility.
        pages = download_info.get('pages', [])
        if not pages:
            # Legacy single-file entry
            fp = download_info.get('file', '')
            if fp:
                pages = [{'file': fp}]

        missing_or_corrupt = []
        for page_info in pages:
            page_path = self.output_dir / page_info['file']
            if not page_path.exists():
                missing_or_corrupt.append(page_info['file'])
            elif not self._is_file_complete(page_path):
                missing_or_corrupt.append(page_info['file'])
                # Remove the corrupt file so it will be re-downloaded
                try:
                    page_path.unlink()
                except OSError:
                    pass

        if missing_or_corrupt:
            self.logger.warning(
                f"{issue_id}: {len(missing_or_corrupt)} of {len(pages)} "
                f"page(s) missing or corrupt -- will re-download"
            )
            del self.metadata['downloaded'][issue_id]
            self._save_metadata()
            return False

        # Also verify completeness flag -- a partial download that was
        # recorded as incomplete should be retried.
        if not download_info.get('complete', True):
            self.logger.info(
                f"{issue_id}: previously incomplete "
                f"({download_info.get('downloaded_pages', '?')}/"
                f"{download_info.get('total_pages', '?')} pages) "
                f"-- will re-download missing pages"
            )
            del self.metadata['downloaded'][issue_id]
            self._save_metadata()
            return False

        return True

    def download_issue(self, issue: Dict) -> bool:
        """Download all page PDFs for a newspaper issue."""
        issue_id = f"{issue['date']}_ed-{issue['edition']}"

        if self._is_download_verified(issue_id):
            self.logger.info(f"Skipping already downloaded issue: {issue_id}")
            self.stats['skipped'] += 1
            return True

        self.logger.info(f"Downloading issue: {issue_id}")

        year_dir = self.output_dir / str(issue['year'])
        year_dir.mkdir(exist_ok=True)

        pdf_files = self._get_pdf_urls_for_issue(issue['item_url'])
        self._rate_limit()

        if not pdf_files:
            self.logger.warning(f"No PDF files found for {issue_id}")
            self.metadata.setdefault('failed', {})[issue_id] = {
                'date': issue['date'],
                'edition': issue['edition'],
                'item_url': issue['item_url'],
                'reason': 'no_pdf_files_found',
                'failed_at': datetime.now().isoformat(),
            }
            self._save_metadata()
            self.stats['failed'] += 1
            return False

        downloaded_pages = []
        all_success = True

        for pdf_info in pdf_files:
            page_num = pdf_info['page']
            page_filename = f"{self.lccn}_{issue_id}_page{page_num:02d}.pdf"
            output_file = year_dir / page_filename

            success = self._download_file(
                pdf_info['url'], output_file,
                expected_size=pdf_info.get('size'),
            )

            if success:
                downloaded_pages.append({
                    'page': page_num,
                    'file': str(output_file.relative_to(self.output_dir)),
                    'size': output_file.stat().st_size,
                })
            else:
                all_success = False
                self.logger.error(f"Failed to download page {page_num} of {issue_id}")

            self._rate_limit()

        if downloaded_pages:
            self.metadata['downloaded'][issue_id] = {
                'date': issue['date'],
                'edition': issue['edition'],
                'pages': downloaded_pages,
                'total_pages': len(pdf_files),
                'downloaded_pages': len(downloaded_pages),
                'complete': all_success,
                'file': downloaded_pages[0]['file'],
                'downloaded_at': datetime.now().isoformat(),
            }
            self.metadata.get('failed', {}).pop(issue_id, None)
            self._save_metadata()
            self.stats['downloaded'] += 1
            self.logger.info(
                f"Downloaded {issue_id}: "
                f"{len(downloaded_pages)}/{len(pdf_files)} pages"
            )
            return True
        else:
            self.metadata.setdefault('failed', {})[issue_id] = {
                'date': issue['date'],
                'edition': issue['edition'],
                'item_url': issue['item_url'],
                'reason': 'all_page_downloads_failed',
                'failed_at': datetime.now().isoformat(),
            }
            self._save_metadata()
            self.stats['failed'] += 1
            self.logger.error(f"Failed to download any pages for: {issue_id}")
            return False

    def _get_retry_failed_issues(self) -> List[Dict]:
        """Build issue list from failed AND partially-downloaded issues."""
        issues = []

        # Explicitly failed issues
        for issue_id, info in self.metadata.get('failed', {}).items():
            year = int(info['date'][:4])
            if self.year_set is None or year in self.year_set:
                issues.append({
                    'date': info['date'],
                    'edition': info['edition'],
                    'item_url': info.get('item_url', info.get('url', '')),
                    'year': year,
                })

        # Issues marked as downloaded but incomplete (some pages missing)
        for issue_id, info in list(self.metadata.get('downloaded', {}).items()):
            if info.get('complete', True):
                continue
            year = int(info['date'][:4])
            if self.year_set is None or year in self.year_set:
                # Need the item_url to re-fetch page list; reconstruct it
                item_url = (
                    f"{BASE_URL}/item/{self.lccn}/"
                    f"{info['date']}/ed-{info['edition']}/"
                )
                issues.append({
                    'date': info['date'],
                    'edition': info['edition'],
                    'item_url': item_url,
                    'year': year,
                })
                # Remove from downloaded so download_issue will re-process
                del self.metadata['downloaded'][issue_id]

        if issues:
            self._save_metadata()
            self.logger.info(f"Found {len(issues)} issues to retry")
        else:
            self.logger.info("No failed or incomplete downloads to retry.")

        return issues

    def run(self):
        """Run the download process."""
        self.logger.info("=" * 70)
        self.logger.info("LOC Newspaper Downloader")
        self.logger.info(f"Newspaper: {self.newspaper_title} (LCCN: {self.lccn})")
        if self.years:
            self.logger.info(f"Years: {min(self.years)}-{max(self.years)}")
        else:
            self.logger.info("Years: all available")
        self.logger.info(f"Output: {self.output_dir.absolute()}")
        self.logger.info(f"Speed: {self.speed_name} ({self.download_delay}s between requests)")
        if self.retry_failed:
            self.logger.info("Mode: Retrying previously failed downloads")
        self.logger.info("=" * 70)

        try:
            disk_usage = shutil.disk_usage(self.output_dir)
            free_gb = disk_usage.free / (1024 ** 3)
            self.logger.info(f"Available disk space: {free_gb:.1f} GB")
            if free_gb < 1.0:
                self.logger.warning("WARNING: Less than 1 GB free disk space!")
        except Exception:
            pass

        start_time = time.time()

        try:
            if self.retry_failed:
                issues = self._get_retry_failed_issues()
            else:
                issues = self._fetch_newspaper_issues()

            if not issues:
                self.logger.warning("No issues found. This might indicate:")
                self.logger.warning("  1. The LCCN is incorrect or not in Chronicling America")
                self.logger.warning("  2. The newspaper has not been digitized for these years")
                self.logger.warning("  3. Network connectivity issues")
                if self.retry_failed:
                    self.logger.warning("  4. No previously failed downloads exist")
                self.logger.info(
                    f"\nVerify at: {BASE_URL}/item/{self.lccn}/"
                )
                return

            total_issues = len(issues)
            self.logger.info(f"\nWill process {total_issues} issues...")

            for i, issue in enumerate(issues, 1):
                self.logger.info(
                    f"\n[{i}/{total_issues}] Processing "
                    f"{issue['date']} ed-{issue['edition']}"
                )
                try:
                    self.download_issue(issue)
                except KeyboardInterrupt:
                    self.logger.info("\n\nDownload interrupted by user.")
                    self.logger.info("Run again to resume where you left off.")
                    raise
                except Exception as e:
                    self.logger.error(f"Unexpected error processing issue: {e}")
                    self.stats['failed'] += 1
                    continue

            elapsed = time.time() - start_time
            self.logger.info("\n" + "=" * 70)
            self.logger.info("DOWNLOAD COMPLETE")
            self.logger.info("=" * 70)
            self.logger.info(f"Total issues processed: {total_issues}")
            self.logger.info(f"Downloaded: {self.stats['downloaded']}")
            self.logger.info(f"Skipped (already done): {self.stats['skipped']}")
            self.logger.info(f"Failed: {self.stats['failed']}")
            self.logger.info(
                f"Data downloaded: "
                f"{self.stats['total_bytes'] / 1024 / 1024:.2f} MB"
            )
            self.logger.info(f"Time elapsed: {elapsed / 60:.1f} minutes")
            self.logger.info("=" * 70)

            if self.stats['failed'] > 0:
                self.logger.info(
                    f"\n{self.stats['failed']} issues failed. "
                    f"Re-run with --retry-failed to retry."
                )

        except KeyboardInterrupt:
            self.logger.info("\n\nShutting down gracefully...")
            self._save_metadata()
            sys.exit(0)
        except Exception as e:
            self.logger.error(f"\nFatal error: {e}")
            self._save_metadata()
            import traceback
            traceback.print_exc()
            sys.exit(1)


# ---------------------------------------------------------------------------
# Year-range parsing
# ---------------------------------------------------------------------------

def parse_year_range(year_str: str) -> List[int]:
    """Parse year range string like '1900-1905' or '1900,1902,1905'."""
    years = []
    for part in year_str.split(','):
        part = part.strip()
        if '-' in part:
            start, end = part.split('-', 1)
            start_val, end_val = int(start.strip()), int(end.strip())
            if end_val < start_val:
                raise ValueError(f"Invalid range: {start_val}-{end_val}")
            years.extend(range(start_val, end_val + 1))
        else:
            years.append(int(part))
    return sorted(set(years))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def cmd_search(args):
    """Handle the --search command."""
    results = search_newspapers(args.search)

    if getattr(args, 'json', False):
        print(json.dumps(results, indent=2))
        return

    if not results:
        print("No newspapers found matching that query.")
        print("Try broader search terms or check spelling.")
        return

    print(f"\nFound {len(results)} newspaper(s):\n")
    print(f"{'LCCN':<16} {'Title':<45} {'Place':<25} {'Dates'}")
    print("-" * 100)
    for r in results:
        title = r['title'][:43] if len(r['title']) > 43 else r['title']
        place = r['place'][:23] if len(r['place']) > 23 else r['place']
        print(f"{r['lccn']:<16} {title:<45} {place:<25} {r['dates']}")

    print(f"\nTo download a newspaper, run:")
    print(f"  python {os.path.basename(__file__)} --lccn <LCCN>")
    print(f"\nExample:")
    if results:
        print(f"  python {os.path.basename(__file__)} --lccn {results[0]['lccn']}")


def cmd_info(args):
    """Handle the --info command."""
    if not getattr(args, 'json', False):
        print(f"Looking up LCCN: {args.info}...")

    info = get_newspaper_info(args.info)

    if getattr(args, 'json', False):
        print(json.dumps(info, indent=2))
        return

    if not info:
        print(f"No newspaper found with LCCN: {args.info}")
        print("Check the LCCN and try again.")
        return

    print(f"\n{'Title:':<15} {info['title']}")
    print(f"{'LCCN:':<15} {info['lccn']}")
    print(f"{'Location:':<15} {info['place']}")
    if info['start_year'] and info['end_year']:
        print(f"{'Date range:':<15} {info['start_year']}-{info['end_year']}")
    print(f"{'URL:':<15} {info['url']}")
    print(f"\nTo download, run:")
    yr = ""
    if info['start_year'] and info['end_year']:
        yr = f" --years {info['start_year']}-{info['end_year']}"
    print(f"  python {os.path.basename(__file__)} --lccn {info['lccn']}{yr}")


def cmd_download(args):
    """Handle the download command."""
    # Validate LCCN format
    if not validate_lccn(args.lccn):
        print(f"Warning: '{args.lccn}' doesn't look like a standard LCCN "
              f"(expected format like 'sn87080287').")
        print("Continuing anyway -- if no results are found, double-check at:")
        print("  https://chroniclingamerica.loc.gov/")

    years = None
    if args.years:
        try:
            years = parse_year_range(args.years)
            if not years:
                print("Error: No valid years specified.")
                sys.exit(1)
        except ValueError as e:
            print(f"Error parsing years: {e}")
            sys.exit(1)

    output = args.output or os.path.join(DOWNLOAD_DIR, args.lccn)
    speed = getattr(args, 'speed', DEFAULT_SPEED)

    downloader = DownloadManager(
        lccn=args.lccn,
        output_dir=output,
        years=years,
        verbose=args.verbose,
        retry_failed=args.retry_failed,
        speed=speed,
    )
    downloader.run()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Download historical newspapers from the Library of Congress '
                    'Chronicling America collection.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:

  Search for a newspaper by name:
    python {os.path.basename(__file__)} --search "Evening Star"

  Show info about a newspaper by LCCN:
    python {os.path.basename(__file__)} --info sn83045462

  Download all available issues:
    python {os.path.basename(__file__)} --lccn sn83045462

  Download specific years:
    python {os.path.basename(__file__)} --lccn sn83045462 --years 1900-1905

  Download to a custom folder:
    python {os.path.basename(__file__)} --lccn sn83045462 --output my_papers

  Retry failed and partial downloads:
    python {os.path.basename(__file__)} --lccn sn83045462 --retry-failed

  Use faster download speed (still within LOC limits):
    python {os.path.basename(__file__)} --lccn sn83045462 --speed standard

  Get search results as JSON (for scripts / GUI):
    python {os.path.basename(__file__)} --search "Evening Star" --json

Common LCCNs:
  sn87080287  Freeland Tribune (Freeland, PA) 1888-1921
  sn83045462  Evening Star (Washington, DC) 1854-1972
  sn83030214  New-York Tribune (New York, NY) 1866-1924

Notes:
  - The script resumes automatically if interrupted (Ctrl+C)
  - Already downloaded files are skipped on re-run
  - Rate limiting is built in to respect LOC servers
  - Find LCCNs at: https://chroniclingamerica.loc.gov/
        """
    )

    # Mutually informative group
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        '--search', '-s',
        type=str, metavar='QUERY',
        help='Search for newspapers by name (e.g., "Evening Star")',
    )
    action.add_argument(
        '--info', '-i',
        type=str, metavar='LCCN',
        help='Show info about a newspaper by LCCN',
    )
    action.add_argument(
        '--lccn', '-l',
        type=str, metavar='LCCN',
        help='LCCN of the newspaper to download (e.g., sn87080287)',
    )

    parser.add_argument(
        '--years', '-y',
        type=str,
        help='Year range to download (e.g., "1900-1905" or "1900,1903,1910")',
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        help=f'Output directory (default: {DOWNLOAD_DIR}/<lccn>)',
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose/debug logging',
    )
    parser.add_argument(
        '--retry-failed',
        action='store_true',
        help='Retry failed and partially-downloaded issues',
    )
    parser.add_argument(
        '--speed',
        choices=list(SPEED_PROFILES.keys()),
        default=DEFAULT_SPEED,
        help=f'Download speed profile (default: {DEFAULT_SPEED}). '
             f'"safe" = 15s delay, "standard" = 4s delay.',
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Output results as JSON (for --search and --info)',
    )

    args = parser.parse_args()

    # Dispatch to the right command
    if args.search:
        cmd_search(args)
    elif args.info:
        cmd_info(args)
    elif args.lccn:
        cmd_download(args)
    else:
        parser.print_help()
        print(f"\nQuick start: python {os.path.basename(__file__)} "
              f"--search \"newspaper name\"")
        sys.exit(1)


if __name__ == '__main__':
    main()
