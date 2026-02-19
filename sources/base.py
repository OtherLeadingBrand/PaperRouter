from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Any
import logging

@dataclass
class IssueMetadata:
    date: str          # YYYY-MM-DD
    edition: int
    url: str
    year: int
    lccn: str
    title: str = ""
    pages: List['PageMetadata'] = field(default_factory=list)

@dataclass
class PageMetadata:
    issue_date: str
    edition: int
    page_num: int
    url: str           # The page item URL
    pdf_url: str = ""
    ocr_url: str = ""  # For external OCR (like LOC LOC OCR)
    lccn: str = ""

@dataclass
class DownloadResult:
    success: bool
    path: Optional[Path] = None
    error: Optional[str] = None
    size_bytes: int = 0

@dataclass
class OCRResult:
    success: bool
    text_path: Optional[Path] = None
    word_count: int = 0
    error: Optional[str] = None

@dataclass
class TitleResult:
    lccn: str
    title: str
    place: str = ""
    dates: str = ""
    url: str = ""

class NewspaperSource(ABC):
    """Abstract base class for all newspaper archive sources (LOC, Trove, etc.)"""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)

    @property
    @abstractmethod
    def name(self) -> str:
        """The internal identifier for the source (e.g. 'loc')"""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """The human-readable name of the source (e.g. 'Library of Congress')"""
        pass

    @abstractmethod
    def fetch_issues(self, lccn: str, year_set: Optional[Set[int]] = None) -> List[IssueMetadata]:
        """Fetch all available issues for a given LCCN, optionally filtered by years."""
        pass

    @abstractmethod
    def get_pages_for_issue(self, issue: IssueMetadata) -> List[PageMetadata]:
        """Fetch metadata for all pages in a specific issue."""
        pass

    @abstractmethod
    def download_page_pdf(self, page: PageMetadata, dest_path: Path) -> DownloadResult:
        """Download the PDF for a specific page."""
        pass

    @abstractmethod
    def fetch_ocr_text(self, page: PageMetadata, output_dir: Path) -> OCRResult:
        """Fetch source-provided OCR text (if available) for a page."""
        pass

    @abstractmethod
    def search_titles(self, query: str) -> List[TitleResult]:
        """Search for newspapers matching the query."""
        pass

    def build_page_url(self, lccn: str, date: str, edition: int, page_num: int) -> str:
        """Build a URL for a specific page. Used for OCR batch reconstruction."""
        return ""
