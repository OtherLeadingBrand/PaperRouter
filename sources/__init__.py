from .base import NewspaperSource, IssueMetadata, PageMetadata, TitleResult, DownloadResult, OCRResult
from .loc_source import LOCSource

SOURCES = {
    'loc': LOCSource,
}

def get_source(name: str, logger=None):
    source_class = SOURCES.get(name.lower())
    if not source_class:
        raise ValueError(f"Unknown source: {name}")
    return source_class(logger=logger)
