# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1-alpha] - 2026-02-27

### Fixed
- **LOC Source Pagination**: Resolved an issue where newspaper issue discovery was capped at 100 results due to a misunderstanding of the Library of Congress API's pagination parameters. Discovery now works across multiple pages, enabling the download of complete collections (e.g., Freeland Tribune's full 1461 issues).
- **Date Range Preview**: Fixed the metadata lookup in the UI to correctly identify and display the full available date range for a publication, instead of only the first page's range.
- **Improved Source Reliability**: Increased the delay between parallel API requests to the Library of Congress to 3 seconds, reducing the frequency of rate-limiting (429) errors during large discovery tasks.

### Changed
- Updated `.gitignore` to exclude temporary reproduction and debug artifacts.

## [0.2.0-alpha] - 2026-02-23

### Added
- Phase 3: OCR Manager initial implementation.
- Date-targeted OCR processing.
- Metadata API upgrades for OCR coverage reporting.
