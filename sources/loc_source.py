import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Any
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from .base import NewspaperSource, IssueMetadata, PageMetadata, DownloadResult, OCRResult, TitleResult

class LOCSource(NewspaperSource):
    """Source implementation for the Library of Congress (Chronicling America)."""
    
    BASE_URL = "https://www.loc.gov"
    COLLECTION_API_URL = f"{BASE_URL}/collections/chronicling-america/"
    API_PAGE_SIZE = 100
    MAX_RETRIES = 5
    RETRY_BACKOFF = 2

    def __init__(self, logger: Optional[logging.Logger] = None):
        super().__init__(logger)
        self.session = self._create_session()

    @property
    def name(self) -> str:
        return "loc"

    @property
    def display_name(self) -> str:
        return "Library of Congress"

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=self.MAX_RETRIES,
            backoff_factor=self.RETRY_BACKOFF,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({
            'User-Agent': 'LOCNewspaperDownloader/2.0 (educational research)'
        })
        self.logger.debug("Created requests session with retry strategy.")
        return session

    def fetch_issues(self, lccn: str, year_set: Optional[Set[int]] = None) -> List[IssueMetadata]:
        self.logger.info(f"Fetching issue list for LCCN {lccn} from LOC...")
        
        all_issues = []
        
        # Optimization: If years are specified, use the dates= filter to reduce pagination
        # Note: dates=YYYY or dates=YYYY/YYYY works in the collection API
        date_filter = ""
        if year_set and len(year_set) > 0:
            min_year = min(year_set)
            max_year = max(year_set)
            if min_year == max_year:
                date_filter = f"&dates={min_year}"
            else:
                date_filter = f"&dates={min_year}/{max_year}"
            self.logger.info(f"  Using year filter: {date_filter}")

        api_url = (
            f"{self.COLLECTION_API_URL}"
            f"?fa=number_lccn:{lccn}{date_filter}"
            f"&c={self.API_PAGE_SIZE}&fo=json"
        )

        page_num = 0
        while api_url:
            page_num += 1
            self.logger.info(f"  Fetching page {page_num} of issue list...")

            try:
                response = self.session.get(api_url, timeout=30)
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                self.logger.error(f"Failed to fetch issue list (page {page_num}): {e}")
                break

            results = data.get('results', [])
            pagination = data.get('pagination', {})

            for item in results:
                item_date = item.get('date', '')
                if not item_date or len(item_date) < 8:
                    continue

                try:
                    year = int(item_date[:4])
                except (ValueError, IndexError):
                    continue

                if year_set and year not in year_set:
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

                all_issues.append(IssueMetadata(
                    date=item_date,
                    edition=edition,
                    url=item_url,
                    year=year,
                    lccn=lccn,
                    title=item.get('title', '').strip().rstrip('.')
                ))

            next_url = pagination.get('next') if isinstance(pagination, dict) else None
            api_url = next_url if next_url else None
            
            # Simple rate limiting for scanning
            if api_url:
                time.sleep(2.0)

        all_issues.sort(key=lambda x: (x.date, x.edition))
        self.logger.info(f"Found {len(all_issues)} issues matching criteria.")
        return all_issues

    def get_pages_for_issue(self, issue: IssueMetadata) -> List[PageMetadata]:
        self.logger.info(f"Fetching page metadata for issue {issue.date}...")
        
        # Ensure URL ends with fo=json
        sep = '&' if '?' in issue.url else '?'
        api_url = f"{issue.url}{sep}fo=json"
        
        try:
            response = self.session.get(api_url, timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            self.logger.error(f"Failed to fetch issue detail: {e}")
            return []

        pages = []
        # LOC API: inner 'resources' list contains the pages
        resources = data.get('resources', [])
        for i, res in enumerate(resources):
            # For each resource, we need to find the PDF and OCR links
            # Usually one 'resource' per page
            page_url = res.get('url') or res.get('id')
            if not page_url:
                continue
                
            # Prefer /resource/ endpoint which has better fulltext metadata
            if '/item/' in page_url:
                page_url = page_url.replace('/item/', '/resource/')

            pages.append(PageMetadata(
                issue_date=issue.date,
                edition=issue.edition,
                page_num=i + 1,
                url=page_url,
                lccn=issue.lccn
            ))
            
        return pages

    def download_page_pdf(self, page: PageMetadata, dest_path: Path) -> DownloadResult:
        self.logger.info(f"Downloading PDF for page {page.page_num}...")
        
        # We might not have the PDF URL yet, so fetch page JSON if needed
        if not page.pdf_url:
            sep = '&' if '?' in page.url else '?'
            api_url = f"{page.url}{sep}fo=json"
            try:
                resp = self.session.get(api_url, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                
                # Mode 1: New resource-style metadata
                res_data = data.get('resource', {})
                if res_data.get('pdf'):
                    page.pdf_url = res_data['pdf']
                
                # Mode 2: Older files-style metadata
                if not page.pdf_url:
                    files = data.get('files', [])
                    for f in files:
                        if f.get('mimetype') == 'application/pdf':
                            page.pdf_url = f.get('url')
                            break
                            
                if not page.pdf_url:
                    # Fallback: resource ID with .pdf
                    page.pdf_url = page.url.rstrip('/') + '.pdf'
            except Exception as e:
                return DownloadResult(success=False, error=f"Metadata fetch failed: {e}")

        try:
            # Handle possible absolute/relative URL issues
            target_url = page.pdf_url
            if target_url.startswith('//'):
                target_url = 'https:' + target_url
            elif target_url.startswith('/'):
                target_url = self.BASE_URL + target_url

            resp = self.session.get(target_url, stream=True, timeout=60)
            resp.raise_for_status()
            
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with open(dest_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        size += len(chunk)
            
            return DownloadResult(success=True, path=dest_path, size_bytes=size)
        except Exception as e:
            return DownloadResult(success=False, error=str(e))

    def fetch_ocr_text(self, page: PageMetadata, output_dir: Path) -> OCRResult:
        self.logger.info(f"Fetching LOC OCR for page {page.page_num}...")
        
        # Fetch page JSON to find fulltext service
        sep = '&' if '?' in page.url else '?'
        api_url = f"{page.url}{sep}fo=json"
        
        try:
            resp = self.session.get(api_url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            ocr_url = None
            # Mode 1: resource.fulltext_file
            res_data = data.get('resource', {})
            if res_data.get('fulltext_file'):
                ocr_url = res_data['fulltext_file']
            
            # Mode 2: top-level fulltext_service
            if not ocr_url:
                ocr_url = data.get('fulltext_service')
                
            if not ocr_url:
                return OCRResult(success=False, error="No OCR service found for this page")
                
            # Handle absolute/relative
            if ocr_url.startswith('//'):
                ocr_url = 'https:' + ocr_url
            elif ocr_url.startswith('/') and not ocr_url.startswith('http'):
                ocr_url = f"{self.BASE_URL}{ocr_url}"
                
            # Append full_text=1 if missing and it's a word-coordinates-service
            if 'word-coordinates-service' in ocr_url and 'full_text=1' not in ocr_url:
                connector = '&' if '?' in ocr_url else '?'
                ocr_url += f'{connector}full_text=1'
                
            resp = self.session.get(ocr_url, timeout=30)
            resp.raise_for_status()
            ocr_data = resp.json()
            
            # The JSON is keyed by the segment ID
            key = next(iter(ocr_data))
            raw_text = ocr_data[key].get('full_text', '')
            
            if not raw_text:
                return OCRResult(success=False, error="OCR text is empty")
                
            # Post-process
            processed_text = self._postprocess_loc_text(raw_text)
            
            # Save
            filename = f"{page.issue_date}_ed-{page.edition}_page{page.page_num:02d}_loc.txt"
            dest_path = output_dir / filename
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Add header
            header = (
                f"# OCR Text — {page.lccn} — {page.issue_date}\n"
                f"# Page: {page.page_num}\n"
                f"# OCR Method: loc-api\n"
                f"# ---\n\n"
            )
            
            with open(dest_path, 'w', encoding='utf-8') as f:
                f.write(header + processed_text)
                
            word_count = len(processed_text.split())
            return OCRResult(success=True, text_path=dest_path, word_count=word_count)

        except Exception as e:
            return OCRResult(success=False, error=str(e))

    def _postprocess_loc_text(self, text: str) -> str:
        """Ported from ocr_engine.py"""
        lines = text.split('\n')
        result_lines = []
        
        last_was_heading = False
        
        for line in lines:
            trimmed = line.strip()
            if not trimmed:
                continue
            
            # Filter artifact lines
            if len(trimmed) == 1 and trimmed in '|iIlj[](){}<>\\/':
                continue
            
            # Heading detection
            is_heading = (len(trimmed) > 3 and trimmed.isupper() and not any(c.isdigit() for c in trimmed))
            if is_heading and not last_was_heading and result_lines:
                result_lines.append("")
                
            result_lines.append(line)
            last_was_heading = is_heading

        processed = "\n".join(result_lines)
        # Join hyphens
        processed = re.sub(r'(\w+)-\n\s*([a-z]\w*)', r'\1\2', processed)
        
        return processed

    def search_titles(self, query: str) -> List[TitleResult]:
        api_url = f"{self.COLLECTION_API_URL}?q={query}&fo=json&fa=original_format:newspaper"
        try:
            resp = self.session.get(api_url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            results = []
            for item in data.get('results', []):
                results.append(TitleResult(
                    lccn=item.get('lccn', ''),
                    title=item.get('title', ''),
                    place=item.get('place_of_publication', ''),
                    dates=item.get('date', ''),
                    url=item.get('url', '')
                ))
            return results
        except Exception as e:
            self.logger.error(f"Search failed: {e}")
            return []

import time # Added missing import
