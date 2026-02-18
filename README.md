# LOC Newspaper Downloader

A robust Python tool to download historical newspaper editions from the **Library of Congress [Chronicling America](https://chroniclingamerica.loc.gov/)** collection.

Supports **any newspaper** in the collection -- just provide the LCCN identifier.

## Features

- **Any Newspaper** -- search by name, look up by LCCN, or browse [Chronicling America](https://chroniclingamerica.loc.gov/)
- **Network Resilience** -- automatic retries with exponential backoff on failures
- **Stop / Resume** -- interrupt any time; re-run to pick up where you left off
- **Multi-page Verification** -- validates every page of every issue on disk; detects and re-downloads missing or corrupt pages
- **Year Filtering** -- download all years or specify a range (e.g. `1900-1905`)
- **Speed Profiles** -- `--speed safe` (15 s, default) or `--speed standard` (4 s) between requests
- **Retry Failed** -- `--retry-failed` re-attempts failed issues **and** partially-downloaded issues with missing pages
- **JSON Output** -- `--json` flag for `--search` and `--info` (for scripts and GUI integration)
- **LCCN Validation** -- warns on non-standard LCCN format before querying
- **Graphical Interface** -- optional GUI with progress bar, search, and LCCN lookup
- **Windows Friendly** -- batch-file launchers, auto-installs dependencies
- **Metadata Tracking** -- JSON progress file with atomic writes and automatic backup
- **Detailed Logging** -- console + log file, verbose mode available

## Quick Start

### 1. Install Python

Download from [python.org](https://www.python.org/downloads/).
**Check "Add Python to PATH"** during installation.

### 2. Install dependencies

```
pip install -r requirements.txt
```

Or just double-click `run.bat` / `run_gui.bat` -- they install dependencies automatically.

### 3. Find your newspaper

```bash
# Search by name
python downloader.py --search "Freeland Tribune"

# Show details for an LCCN
python downloader.py --info sn87080287
```

### 4. Download

```bash
# Download all available issues
python downloader.py --lccn sn87080287

# Download specific years
python downloader.py --lccn sn87080287 --years 1900-1905

# Download multiple ranges
python downloader.py --lccn sn87080287 --years 1900-1905,1910,1915-1920

# Custom output directory
python downloader.py --lccn sn87080287 --output my_papers

# Retry only previously failed issues
python downloader.py --lccn sn87080287 --retry-failed
```

### GUI (easiest)

Double-click **`run_gui.bat`** to open the graphical interface. Enter an LCCN or search by name, pick your options, and click **Start Download**.

## Example Newspapers

| LCCN | Title | Location | Dates |
|------|-------|----------|-------|
| `sn87080287` | Freeland Tribune | Freeland, PA | 1888-1921 |
| `sn83045462` | Evening Star | Washington, DC | 1854-1972 |
| `sn83030214` | New-York Tribune | New York, NY | 1866-1924 |
| `sn83030213` | New-York Daily Tribune | New York, NY | 1842-1866 |
| `sn82015775` | The Topeka Daily Capital | Topeka, KS | 1892-1901 |

Browse the full catalogue at [Chronicling America](https://chroniclingamerica.loc.gov/).

## CLI Reference

```
usage: downloader.py [-h] [--search QUERY | --info LCCN | --lccn LCCN]
                     [--years YEARS] [--output OUTPUT] [--verbose]
                     [--retry-failed] [--speed {safe,standard}] [--json]

  --search QUERY, -s    Search for newspapers by name
  --info LCCN, -i       Show info about a newspaper by LCCN
  --lccn LCCN, -l       Download a newspaper by LCCN

  --years YEARS, -y     Year range (e.g. "1900-1905" or "1900,1903,1910")
  --output OUTPUT, -o   Output directory (default: downloads/<lccn>)
  --verbose, -v         Enable verbose logging
  --retry-failed        Retry failed AND partially-downloaded issues
  --speed {safe,standard}
                        Download speed profile (default: safe)
                        safe = 15 s delay, standard = 4 s delay
  --json                Output search/info results as JSON
```

## Output Structure

```
downloads/
  sn87080287/
    1900/
      sn87080287_1900-01-08_ed-1_page01.pdf
      sn87080287_1900-01-08_ed-1_page02.pdf
      ...
    1901/
      ...
    download_metadata.json
    download.log
```

## How It Works

1. **Queries the LOC API** to discover all digitized issues for the newspaper LCCN
2. **Paginates efficiently** -- ~15 API requests covers ~1,500 issues (vs. brute-force)
3. **Fetches PDF URLs** from each issue's JSON metadata
4. **Downloads per-page PDFs** with streaming, temp files, and validation
5. **Tracks progress** in `download_metadata.json` so interrupted runs resume cleanly

## Requirements

- Python 3.7+
- `requests` library (auto-installed by batch launchers)
- Internet connection
- Disk space varies by newspaper (a few hundred MB to several GB)

## Rate Limits

The LOC API enforces:
- **Burst:** 20 requests / minute (block 5 min)
- **Crawl:** 20 requests / 10 seconds (block 1 hour)

Both built-in speed profiles stay within these limits:

| Profile | Delay | Requests/min | Use case |
|---------|-------|-------------|----------|
| `safe` (default) | 15 s | ~4 | Safest; good for unattended runs |
| `standard` | 4 s | ~15 | Faster; still within burst limit |

**Please do not** run multiple instances simultaneously or bypass the rate limiter.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Python is not recognized` | Reinstall Python; check "Add Python to PATH" |
| `No module named 'requests'` | Run `pip install requests` |
| `No issues found` | Verify LCCN at [loc.gov](https://www.loc.gov/) -- not all newspapers are digitized |
| Download interrupted | Re-run the same command; it resumes automatically |
| Slow downloads | Normal -- rate limiting protects LOC servers |
| Corrupt files | Re-run; invalid PDFs are detected and re-downloaded |

## License

[MIT](LICENSE)

Newspapers in Chronicling America are in the **public domain** (no known copyright restrictions).

## Links

- [Chronicling America](https://chroniclingamerica.loc.gov/)
- [LOC API Documentation](https://www.loc.gov/apis/)
- [Chronicling America API Guide](https://guides.loc.gov/chronicling-america/additional-features)
- [LOC API Rate Limits](https://www.loc.gov/apis/json-and-yaml/working-within-limits/)
