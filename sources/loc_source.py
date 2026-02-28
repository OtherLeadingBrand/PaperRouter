import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

        # 1. Fetch first page to get total count
        try:
            response = self.session.get(api_url, timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            self.logger.error(f"Failed to fetch issue list (initial page): {e}")
            return []

        results = data.get('results', [])
        pagination = data.get('pagination', {})
        total_items = pagination.get('of', pagination.get('total', 0))
        
        def process_json_data(data):
            batch = []
            for item in data.get('results', []):
                item_date = item.get('date', '')
                if not item_date or len(item_date) < 8:
                    continue
                try:
                    year = int(item_date[:4])
                except (ValueError, IndexError):
                    self.logger.warning(f"  Skipping item with malformed date: '{item_date}' (URL: {item.get('url')})")
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
                batch.append(IssueMetadata(
                    date=item_date, edition=edition, url=item_url,
                    year=year, lccn=lccn,
                    title=item.get('title', '').strip().rstrip('.')
                ))
            return batch

        all_issues.extend(process_json_data(data))

        # 2. Parallel fetch remaining pages
        total_pages = pagination.get('total', 1)
        if total_pages > 1:
            self.logger.info(f"  Parallel fetching {total_pages - 1} remaining pages...")
            
            import threading
            fetch_lock = threading.Lock()
            last_fetch_time = [time.time()]
            
            def fetch_page(p):
                with fetch_lock:
                    now = time.time()
                    sleep_time = max(0, 3.0 - (now - last_fetch_time[0]))
                    last_fetch_time[0] = now + sleep_time
                if sleep_time > 0:
                    time.sleep(sleep_time)

                # The LOC API uses 'sp' as the page number for this endpoint.
                page_url = f"{api_url}&sp={p}"
                try:
                    resp = self.session.get(page_url, timeout=30)
                    resp.raise_for_status()
                    batch = process_json_data(resp.json())
                    return batch
                except Exception as e:
                    self.logger.error(f"    Failed to fetch page {p}: {e}")
                    return []

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(fetch_page, p) for p in range(2, total_pages + 1)]
                for future in as_completed(futures):
                    all_issues.extend(future.result())

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
        # LOC API structure: resources[0] contains the issue with a 'files' list.
        # Each entry in 'files' is a page, represented as a list of file variants
        # (PDF, JP2, XML, etc. for that page).
        resources = data.get('resources', [])
        if not resources:
            return pages

        resource = resources[0]
        resource_url = resource.get('url', issue.url).rstrip('/')
        file_groups = resource.get('files', [])

        for i, file_group in enumerate(file_groups):
            page_num = i + 1
            # Build a page-specific URL using ?sp=N
            page_url = f"{resource_url}?sp={page_num}"

            # Extract the PDF URL from this page's file variants
            pdf_url = ""
            for entry in file_group:
                if entry.get('mimetype') == 'application/pdf':
                    pdf_url = entry.get('url', '')
                    break

            pages.append(PageMetadata(
                issue_date=issue.date,
                edition=issue.edition,
                page_num=page_num,
                url=page_url,
                pdf_url=pdf_url,
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

    def build_page_url(self, lccn: str, date: str, edition: int, page_num: int) -> str:
        """Build a LOC resource URL for a specific page."""
        return f"{self.BASE_URL}/resource/{lccn}/{date}/ed-{edition}/?sp={page_num}"

    def search_titles(self, query: str) -> List[TitleResult]:
        api_url = f"{self.COLLECTION_API_URL}?q={query}&fo=json&fa=original_format:newspaper"
        try:
            resp = self.session.get(api_url, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            seen_lccns = set()
            results = []
            for item in data.get('results', []):
                # LOC API uses 'number_lccn' (a list) rather than 'lccn'
                lccn_list = item.get('number_lccn', [])
                lccn = lccn_list[0] if lccn_list else ''
                if not lccn or lccn in seen_lccns:
                    continue
                seen_lccns.add(lccn)

                # partof_title has the newspaper name; fall back to item title
                partof = item.get('partof_title', [])
                title = partof[0] if partof else item.get('title', '')

                # Location from composite_location or location fields
                location = ''
                loc_state = item.get('location_state', [])
                loc_city = item.get('location_city', [])
                if loc_city and loc_state:
                    location = f"{loc_city[0]}, {loc_state[0]}"
                elif loc_state:
                    location = loc_state[0]

                dates = item.get('date', '')
                if isinstance(dates, list):
                    dates = dates[0] if dates else ''

                # Use the first thumbnail image if available
                image_url = item.get('image_url', [])
                thumbnail = image_url[0] if image_url else ''

                results.append(TitleResult(
                    lccn=lccn,
                    title=title,
                    place=location,
                    dates=dates,
                    url=item.get('url', ''),
                    thumbnail=thumbnail
                ))
            return results
        except Exception as e:
            self.logger.error(f"Search failed: {e}")
            return []

    def get_details(self, lccn: str) -> Optional[Dict]:
        """Fetch basic metadata for an LCCN using the search API with an lccn filter."""
        results = self.search_titles(f'lccn:"{lccn}"')
        if not results:
            return None

        # Use the first match
        r = results[0]

        # Get accurate date range by sampling the actual issues collection
        start_year = None
        end_year = None
        try:
            # Fetch first and last pages of the issues collection to find min/max dates
            api_url = f"{self.COLLECTION_API_URL}?fa=number_lccn:{lccn}&c=1&fo=json"

            # Get first page (oldest)
            resp = self.session.get(api_url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            results_list = data.get('results', [])
            if results_list:
                first_date = results_list[0].get('date', '')
                if first_date and len(first_date) >= 4:
                    start_year = int(first_date[:4])

            # Get total count to fetch last page (newest)
            pagination = data.get('pagination', {})
            total_items = pagination.get('of', pagination.get('total', 0))
            if total_items > 0:
                if total_items > 1:
                    # With c=1, each page has one item, so sp=total_items gets the last page/item.
                    last_url = f"{api_url}&sp={total_items}"
                    resp = self.session.get(last_url, timeout=30)
                    resp.raise_for_status()
                    data = resp.json()
                    results_list = data.get('results', [])
                    if results_list:
                        # Get the last item on the last page
                        last_date = results_list[-1].get('date', '')
                        if last_date and len(last_date) >= 4:
                            end_year = int(last_date[:4])
                else:
                    # Only one page, so last item is on first page
                    if results_list:
                        last_date = results_list[-1].get('date', '')
                        if last_date and len(last_date) >= 4:
                            end_year = int(last_date[:4])
        except Exception as e:
            self.logger.warning(f"Could not fetch accurate date range for {lccn}: {e}")
            # Fallback to extracting from search result dates string
            if r.dates:
                try:
                    found_years = re.findall(r'\d{4}', r.dates)
                    if len(found_years) >= 1 and not start_year:
                        start_year = int(found_years[0])
                    if len(found_years) >= 2 and not end_year:
                        end_year = int(found_years[1])
                except:
                    pass

        return {
            'lccn': r.lccn,
            'title': r.title,
            'place': r.place,
            'start_year': start_year,
            'end_year': end_year,
            'url': r.url,
            'thumbnail': r.thumbnail
        }

