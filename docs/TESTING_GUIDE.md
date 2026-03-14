# PaperRouter Manual Testing Guide

**Audience:** Human tester or automated test agent (e.g. Gemini Flash 3)
**Scope:** Validates the three change areas introduced in this release:

1. **OCR text download resume** (root cause fix) - `downloader.py`, `ocr_engine.py`
2. **Metadata save order** (crash resilience) - `downloader.py`
3. **Startup dependency check** (UX) - `gui.py`

> **API Reference:** PaperRouter uses the [Library of Congress loc.gov JSON API](https://www.loc.gov/apis/json-and-yaml/) to access the
> [Chronicling America](https://www.loc.gov/collections/chronicling-america/) newspaper collection.
> The API is public, requires no key, but enforces dynamic rate limits that return HTTP 429 when exceeded.
> Full OCR text is available in the `full_text` JSON field per page.
> See [Working Within Limits](https://www.loc.gov/apis/json-and-yaml/working-within-limits/) for current rate-limit guidance.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Environment Setup](#environment-setup)
- [Test Suite A: Startup Dependency Check](#test-suite-a-startup-dependency-check)
- [Test Suite B: OCR Resume — CLI Path](#test-suite-b-ocr-resume--cli-path)
- [Test Suite C: OCR Resume — OCR Batch Path](#test-suite-c-ocr-resume--ocr-batch-path)
- [Test Suite D: Metadata Crash Resilience](#test-suite-d-metadata-crash-resilience)
- [Test Suite E: OCR Failure Logging](#test-suite-e-ocr-failure-logging)
- [Test Suite F: Upgrade Path (Old Metadata)](#test-suite-f-upgrade-path-old-metadata)
- [Test Suite G: Force-OCR Flag](#test-suite-g-force-ocr-flag)
- [Test Suite H: GUI Smoke Test](#test-suite-h-gui-smoke-test)
- [Test Suite I: Web GUI Smoke Test](#test-suite-i-web-gui-smoke-test)
- [Quick Reference: Key File Paths](#quick-reference-key-file-paths)
- [Appendix: Known LCCNs for Testing](#appendix-known-lccns-for-testing)

---

## Prerequisites

- Python 3.7 or later
- Internet connection (tests hit the live LOC API)
- A clean directory to copy the project into (simulates a fresh install)
- Approximately 50 MB of free disk space for test downloads

**Test LCCN used throughout:** `sn87080287` (Freeland Tribune, PA — small collection, fast to download)

---

## Environment Setup

These steps simulate a user copying PaperRouter to a new machine.

```bash
# 1. Create a fresh test directory
mkdir paperrouter-test
cd paperrouter-test

# 2. Copy ALL project files here (simulating a download/extract).
#    Exclude .venv/, .git/, downloads/, __pycache__/
#    On Windows:
robocopy C:\source\PaperRouter . /E /XD .venv .git downloads __pycache__
#    On Linux/macOS:
#    rsync -av --exclude='.venv' --exclude='.git' --exclude='downloads' --exclude='__pycache__' /source/PaperRouter/ .

# 3. Create a fresh virtual environment
python -m venv .venv

# 4. Activate it
#    Windows:
.venv\Scripts\activate
#    Linux/macOS:
#    source .venv/bin/activate

# 5. DO NOT install requirements yet — Test Suite A validates the auto-installer.
```

> **Checkpoint:** You should now have an activated venv with NO packages installed
> (only pip and setuptools). Verify with `pip list`.

---

## Test Suite A: Startup Dependency Check

**What we are testing:** When the GUI launches in an environment missing `requests`,
`flask`, or `psutil`, it should detect this, list the missing packages, and offer
to install them automatically.

### Test A1: All dependencies missing (Tkinter GUI)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Run `python gui.py` | A dialog appears titled **"Missing Dependencies"** listing all three packages: `requests>=2.31.0`, `flask>=3.0.0`, `psutil>=5.9.0` |
| 2 | Click **Yes** | A second window appears: "Installing packages, please wait..." |
| 3 | Wait for install to complete | The install window closes. The main GUI window opens with title "LOC Newspaper Downloader" |
| 4 | Close the GUI | Exit cleanly |
| 5 | Run `pip list` in the terminal | Confirm `requests`, `flask`, `psutil` are now installed |

**Pass criteria:** All three packages installed without manual intervention; GUI launched successfully.

### Test A2: Partial dependencies missing

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Run `pip uninstall psutil -y` | psutil removed |
| 2 | Run `python gui.py` | Dialog lists only `psutil>=5.9.0` (not requests or flask) |
| 3 | Click **Yes** | psutil installs; GUI opens |

**Pass criteria:** Only the truly missing package is listed and installed.

### Test A3: User declines install

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Run `pip uninstall requests -y` | requests removed |
| 2 | Run `python gui.py` | Missing Dependencies dialog appears |
| 3 | Click **No** | Application exits (return code 1). No packages installed |
| 4 | Run `pip list` | Confirm `requests` is still absent |

**Pass criteria:** Application exits cleanly without installing anything.

### Test A4: All dependencies present (no dialog)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Run `pip install -r requirements.txt` | All packages installed |
| 2 | Run `python gui.py` | GUI opens directly — no dependency dialog shown |

**Pass criteria:** Zero startup delay from dependency checking when everything is present.

---

## Test Suite B: OCR Resume — CLI Path

**What we are testing:** When PDFs are already downloaded and the user re-runs with
`--ocr loc`, the tool should skip PDF re-downloading and only fetch missing OCR text.

### Test B1: Initial download without OCR

```bash
python downloader.py --lccn sn87080287 --years 1900 --max-issues 1 --speed standard --output test_output
```

| Check | Expected |
|-------|----------|
| Console shows `[1/1] Processing ...` | Issue downloads |
| `test_output/1900/` contains `.pdf` files | PDF pages present |
| `test_output/1900/` contains NO `_loc.txt` files | OCR was not requested |
| `test_output/download_metadata.json` exists | Metadata saved |

Open `test_output/download_metadata.json` and verify:
- The issue is in `"downloaded"` with `"complete": true`
- There is **no** `"ocr_complete"` field (OCR was not run)

### Test B2: Re-run with OCR enabled (resume path)

```bash
python downloader.py --lccn sn87080287 --years 1900 --max-issues 1 --speed standard --output test_output --ocr loc
```

| Check | Expected |
|-------|----------|
| Console shows `PDFs already downloaded; resuming OCR for ...` | **Critical:** PDFs are NOT re-downloaded |
| Console does NOT show `Downloading` or byte-size messages | No PDF traffic |
| `test_output/1900/` now contains `_loc.txt` files alongside PDFs | OCR text fetched |
| Each `.txt` file starts with `# OCR Text —` header | Proper format |

Open `test_output/download_metadata.json` and verify:
- `"ocr_complete": true` is now set on the issue entry

### Test B3: Third run skips everything

```bash
python downloader.py --lccn sn87080287 --years 1900 --max-issues 1 --speed standard --output test_output --ocr loc
```

| Check | Expected |
|-------|----------|
| Console shows `Skipping (already complete)` | Both PDFs and OCR are skipped |
| Execution completes in under 5 seconds | No API calls made |

**Pass criteria for B1-B3:** The three-run sequence proves that (1) PDF-only download works,
(2) OCR can be added later without re-downloading PDFs, and (3) completed issues are fully skipped.

### Test B4: Partial OCR recovery

This tests that individual failed OCR pages are retried on next run.

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Delete ONE `_loc.txt` file from `test_output/1900/` (e.g., page02) | One OCR file removed |
| 2 | Re-run the command from B2 | Console shows `PDFs already downloaded; resuming OCR...` |
| 3 | Check the output directory | The deleted file is re-created; all other pages are skipped (debug log) |
| 4 | Check metadata | `"ocr_complete": true` is set again |

**Pass criteria:** Only the missing page is re-fetched; existing pages are not re-downloaded.

---

## Test Suite C: OCR Resume — OCR Batch Path

**What we are testing:** The `--ocr-batch` flag processes previously downloaded issues
using the same consolidated logic.

### Test C1: OCR batch on existing downloads

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Delete ALL `_loc.txt` files from `test_output/1900/` | OCR files removed |
| 2 | Remove `"ocr_complete"` from `download_metadata.json` (edit the file, delete the key) | OCR status reset |
| 3 | Run: `python downloader.py --lccn sn87080287 --years 1900 --output test_output --ocr loc --ocr-batch` | OCR batch starts |
| 4 | Check console output | Shows `STARTING OCR BATCH`, processing message, then `OCR complete for ...` |
| 5 | Check `test_output/1900/` | All `_loc.txt` files re-created |
| 6 | Check metadata | `"ocr_complete": true` set |

### Test C2: OCR batch skips already-complete issues

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Re-run the same command from C1 | All pages skipped (debug log); completes quickly |

### Test C3: OCR batch with --date filter

```bash
# Use a specific date from your downloaded issue (check metadata for exact date)
python downloader.py --lccn sn87080287 --output test_output --ocr loc --ocr-batch --date 1900-01-04
```

| Check | Expected |
|-------|----------|
| Only the targeted issue date is processed | Other dates are skipped |

---

## Test Suite D: Metadata Crash Resilience

**What we are testing:** If the process is killed during OCR (after PDFs download),
the PDF completion is preserved in metadata and a restart resumes OCR without
re-downloading PDFs.

### Test D1: Simulate crash during OCR

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Delete `test_output/` to start fresh | Clean slate |
| 2 | Run: `python downloader.py --lccn sn87080287 --years 1901 --max-issues 1 --speed standard --output test_output --ocr loc` | Download + OCR starts |
| 3 | **While OCR is running** (after PDFs finish), press Ctrl+C or kill the process | Process terminates mid-OCR |
| 4 | Open `test_output/download_metadata.json` | **Critical:** The issue MUST be in `"downloaded"` with `"complete": true` even though OCR was interrupted |
| 5 | Check `test_output/1901/` | PDF files are present. Some `_loc.txt` files may exist (partial OCR) |
| 6 | Re-run the same command | Console shows `PDFs already downloaded; resuming OCR...` |
| 7 | Let it complete | All `_loc.txt` files are now present; `"ocr_complete": true` in metadata |

**Pass criteria:** PDF progress is never lost by an OCR interruption. This was the
core bug — previously, metadata was only saved after OCR finished, so a crash
lost both PDF and OCR progress.

> **Note:** Timing the Ctrl+C requires watching the log output. PDFs download first
> (you'll see `[page N/M] done` messages), then OCR starts (you'll see
> `Fetching LOC OCR for page N...` messages). Press Ctrl+C during the OCR phase.

---

## Test Suite E: OCR Failure Logging

**What we are testing:** When LOC OCR fails for a page, the failure is now logged
(previously it was silent).

### Test E1: Verify failure logging

This test requires triggering an OCR failure, which is hard to do reliably with a
live API. Two approaches:

**Approach 1 — Check log output format:**

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open `ocr_engine.py` | Read lines 173-179 |
| 2 | Verify the `else` clause exists | Code shows: `self.logger.warning(f"  Page {page.page_num} - Tier 1 OCR: Failed: {res.error}")` |

**Approach 2 — Temporary code injection (optional, advanced):**

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | In `sources/loc_source.py`, temporarily change the OCR API URL on line 297 to an invalid URL (e.g., `ocr_url = "https://httpstat.us/500"`) | Forces OCR fetch failure |
| 2 | Delete an existing `_loc.txt` file and re-run with `--ocr loc` | Console shows `Tier 1 OCR: Failed: ...` warning |
| 3 | Check metadata | `"ocr_complete"` is NOT set (because a page failed) |
| 4 | **Revert the code change** | Restore original line 297 |

---

## Test Suite F: Upgrade Path (Old Metadata)

**What we are testing:** When a user upgrades from the previous version, their existing
`download_metadata.json` (which lacks the `ocr_complete` field) works correctly with
the new code.

### Test F1: Simulate pre-upgrade metadata

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Ensure `test_output/` has a completed download with `_loc.txt` files present | From earlier tests |
| 2 | Edit `test_output/download_metadata.json`: remove every `"ocr_complete"` key from all issue entries | Simulates old-format metadata |
| 3 | Run: `python downloader.py --lccn sn87080287 --years 1900 --max-issues 1 --speed standard --output test_output --ocr loc` | Starts processing |
| 4 | Check console | Shows `PDFs already downloaded; resuming OCR...` (because `ocr_complete` is missing) |
| 5 | Check that NO new API calls are made | Pages are skipped because `_loc.txt` files already exist on disk |
| 6 | Check metadata | `"ocr_complete": true` is now set (backfilled by file-existence check) |
| 7 | Re-run the same command | Now shows `Skipping (already complete)` |

**Pass criteria:** Upgrading users experience no disruption. The first run after upgrade
scans existing files and backfills `ocr_complete` transparently. No data is re-downloaded.

---

## Test Suite G: Force-OCR Flag

**What we are testing:** The `--force-ocr` flag re-processes OCR even when
`ocr_complete` is set and text files exist.

### Test G1: Force re-download of OCR text

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Confirm `test_output/download_metadata.json` shows `"ocr_complete": true` for an issue | From prior tests |
| 2 | Note the file modification times of `_loc.txt` files in `test_output/1900/` | Record timestamps |
| 3 | Run: `python downloader.py --lccn sn87080287 --years 1900 --max-issues 1 --output test_output --ocr loc --force-ocr` | Force OCR starts |
| 4 | Check console | Does NOT show `Skipping`; shows `Fetching LOC OCR...` for every page |
| 5 | Check file timestamps | `_loc.txt` files have NEW modification times |
| 6 | Check metadata | `"ocr_complete": true` is re-set after successful completion |

### Test G2: Force-OCR bypasses the downloaded-issue skip gate

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Run the same command as G1 again | The `--force-ocr` flag causes re-entry into `_process_ocr_for_issue` even though the issue was already in `downloaded` |
| 2 | Check that existing `_loc.txt` files are overwritten | File contents may be identical but timestamps update |

---

## Test Suite H: GUI Smoke Test

**What we are testing:** The Tkinter GUI still works correctly after the changes.

### Test H1: Basic GUI workflow

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Run `python gui.py` | GUI opens with "LOC Newspaper Downloader" title |
| 2 | Enter LCCN `sn87080287` and click **Look Up** | Info panel shows "Freeland Tribune" details |
| 3 | Set Years to "1900", Max Issues to "1", OCR to "LOC API Text" | Settings applied |
| 4 | Set output folder to a test location | Folder selected |
| 5 | Click **Start Download** | Progress bar starts (indeterminate, then switches to determinate) |
| 6 | Watch log output area | Shows download progress and OCR processing |
| 7 | Wait for completion | Status shows "Complete"; Start button re-enables |
| 8 | Click **Start Download** again (same settings) | Should skip everything quickly ("already complete") |
| 9 | Close the window | Clean exit, no orphaned processes |

### Test H2: Stop button during download

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Start a download (any settings) | Download begins |
| 2 | Click **Stop** while download is in progress | Download stops; status resets; Start button re-enables |
| 3 | Check that no Python processes are left running | Use Task Manager or `ps aux` |

---

## Test Suite I: Web GUI Smoke Test

**What we are testing:** The Flask web interface still functions correctly.

### Test I1: Basic web workflow

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Run `python web_gui.py` | Terminal shows `Running on http://localhost:5000`; browser opens |
| 2 | In the browser, enter LCCN `sn87080287` | LCCN field accepts input |
| 3 | Click search or lookup | Newspaper info loads |
| 4 | Set Years to "1900", Max Issues to "1", OCR mode to "loc" | Settings configured |
| 5 | Click **Start Download** | Progress updates in real time via log stream |
| 6 | Wait for completion | Status shows complete |
| 7 | Press Ctrl+C in terminal | Flask server shuts down cleanly |

---

## Quick Reference: Key File Paths

| File | Purpose | Changed In This Release |
|------|---------|------------------------|
| `downloader.py` | Core download + OCR engine (CLI) | Yes — skip logic, metadata save order, `_process_ocr_for_issue()` |
| `ocr_engine.py` | OCR orchestration | Yes — added failure logging for LOC OCR |
| `gui.py` | Tkinter desktop GUI | Yes — startup dependency check |
| `web_gui.py` | Flask web GUI | No |
| `sources/loc_source.py` | LOC API integration | No |
| `sources/base.py` | Data classes and abstract base | No |
| `harness.py` | Memory-protected OCR wrapper | No |
| `requirements.txt` | Python dependencies | No |

**Metadata file:** `<output_dir>/download_metadata.json`

Key fields in a downloaded issue entry:
```json
{
  "date": "1900-01-04",
  "edition": 1,
  "complete": true,
  "ocr_complete": true,
  "downloaded_at": "2026-03-14T10:30:00.000000",
  "pages": [
    {"page": 1, "file": "1900/sn87080287_1900-01-04_ed-1_page01.pdf", "size": 45000}
  ]
}
```

**OCR text file naming:** `<date>_ed-<edition>_page<NN>_loc.txt`

---

## Appendix: Known LCCNs for Testing

| LCCN | Title | Notes |
|------|-------|-------|
| `sn87080287` | Freeland Tribune (PA) | Small collection; fast for testing |
| `sn83045462` | Evening Star (DC) | Large collection; good for stress testing |
| `sn83030214` | New-York Tribune (NY) | Large; good for pagination tests |

### LOC API Endpoints Used

- **Issue list:** `https://www.loc.gov/collections/chronicling-america/?fo=json&at=results&q=lccn:sn87080287`
- **Page JSON:** `https://www.loc.gov/resource/sn87080287.1900-01-04.ed-1.seq-1/?fo=json`
- **OCR text:** Retrieved from `fulltext_file` or `fulltext_service` field in page JSON response

### Rate Limits

The [LOC API rate limits](https://www.loc.gov/apis/json-and-yaml/working-within-limits/) are dynamic
and depend on current server load. PaperRouter uses two speed profiles:

- **Safe** (default): 15 seconds between downloads (~4 req/min)
- **Standard**: 4 seconds between downloads (~15 req/min)

For testing, `--speed standard` is recommended to reduce wait times. If you receive
HTTP 429 errors, switch back to `--speed safe`.

---

## Summary: What Each Test Suite Validates

| Suite | Bug / Feature | Files Tested |
|-------|--------------|--------------|
| A | Startup dependency auto-install | `gui.py` |
| B | OCR resume without re-downloading PDFs (root cause) | `downloader.py` |
| C | OCR batch uses consolidated logic | `downloader.py` |
| D | Metadata saved before OCR (crash resilience) | `downloader.py` |
| E | OCR failures are logged (no longer silent) | `ocr_engine.py` |
| F | Old metadata upgraded transparently | `downloader.py` |
| G | `--force-ocr` overrides completion | `downloader.py` |
| H | Tkinter GUI smoke test | `gui.py` |
| I | Web GUI smoke test | `web_gui.py` |

Sources:
- [LOC JSON/YAML API Documentation](https://www.loc.gov/apis/json-and-yaml/)
- [Working Within Limits (Rate Limits)](https://www.loc.gov/apis/json-and-yaml/working-within-limits/)
- [Chronicling America API](https://www.loc.gov/apis/additional-apis/chronicling-america-api/)
- [Chronicling America OCR Data](https://chroniclingamerica.loc.gov/ocr/)
- [Using the loc.gov API with Chronicling America](https://libraryofcongress.github.io/data-exploration/loc.gov%20JSON%20API/Chronicling_America/README.html)
- [Chronicling America Additional Features & API Access](https://guides.loc.gov/chronicling-america/additional-features)
