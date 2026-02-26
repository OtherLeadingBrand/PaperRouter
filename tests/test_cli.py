"""
test_cli.py — Comprehensive CLI integration and unit tests for PaperRouter.

Runs real-world commands against the Library of Congress Chronicling America API.
Reference newspaper: Freeland Tribune (LCCN: sn87080287).

Run from the project root:
    python -m unittest tests/test_cli.py -v

Network tests are automatically skipped if the LOC API is unreachable.
Download tests write to a temporary directory that is cleaned up on teardown.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
DOWNLOADER = str(PROJECT_ROOT / "downloader.py")
HARNESS    = str(PROJECT_ROOT / "harness.py")
PYTHON     = sys.executable

# Well-known newspaper used for all live tests.
KNOWN_LCCN  = "sn87080287"    # Freeland Tribune (Freeland PA)
KNOWN_TITLE = "Freeland"


def run_downloader(*args, env_extra=None, timeout=120):
    """Run downloader.py with the given args. Returns CompletedProcess."""
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [PYTHON, DOWNLOADER] + list(args),
        capture_output=True, text=True,
        cwd=str(PROJECT_ROOT), timeout=timeout, env=env
    )


def run_harness(*args, env_extra=None, timeout=180):
    """Run harness.py with the given args. Returns CompletedProcess."""
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [PYTHON, HARNESS] + list(args),
        capture_output=True, text=True,
        cwd=str(PROJECT_ROOT), timeout=timeout, env=env
    )


def skip_if_network_error(result):
    """
    Call after run_downloader/run_harness. If the combined output indicates a
    connectivity failure, raise SkipTest so the suite keeps running in CI.
    """
    combined = result.stdout + result.stderr
    network_keywords = [
        "ConnectionError", "NewConnectionError", "Failed to establish",
        "Name or service not known", "Max retries exceeded",
        "getaddrinfo failed",
    ]
    for kw in network_keywords:
        if kw in combined:
            raise unittest.SkipTest(f"LOC API unreachable ({kw})")


# ---------------------------------------------------------------------------
# 1. TestSearch
# ---------------------------------------------------------------------------
class TestSearch(unittest.TestCase):
    """Tests for the --search flag."""

    def test_search_plain(self):
        """Plain text search should return results containing the known LCCN."""
        result = run_downloader("--search", "Freeland Tribune")
        skip_if_network_error(result)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn(KNOWN_LCCN, combined,
                      msg=f"Expected LCCN '{KNOWN_LCCN}' not found in output:\n{combined}")

    def test_search_json(self):
        """JSON search should return a parseable list with the known LCCN."""
        result = run_downloader("--search", "Freeland Tribune", "--json")
        skip_if_network_error(result)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            self.fail(f"stdout is not valid JSON: {e}\nOutput:\n{result.stdout}")
        self.assertIsInstance(data, list, "Expected a JSON array")
        lccns = [item.get("lccn") for item in data]
        self.assertIn(KNOWN_LCCN, lccns,
                      msg=f"'{KNOWN_LCCN}' not in returned LCCNs: {lccns}")

    def test_search_no_results(self):
        """A nonsense query should return gracefully with no results (empty table or message)."""
        result = run_downloader("--search", "xyzzy_definitely_not_a_newspaper_12345")
        skip_if_network_error(result)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        combined = result.stdout + result.stderr
        # Rich renders an empty table with just headers; there should be no LCCN data rows.
        # Acceptable outputs: empty JSON list, "No newspapers found", or a table with no data rows.
        # We check that no real LCCN-like token appears (sn/es/wa + digits).
        import re as _re
        lccn_pattern = _re.compile(r'\b[a-z]{1,3}\d{8,10}\b')
        has_results = bool(lccn_pattern.search(combined))
        self.assertFalse(has_results,
                         msg=f"Unexpected LCCN found in empty-search output:\n{combined}")

    def test_search_no_results_json(self):
        """A nonsense query with --json should produce an empty list, not an error."""
        result = run_downloader("--search", "xyzzy_definitely_not_a_newspaper_12345", "--json")
        skip_if_network_error(result)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            self.fail(f"stdout is not valid JSON: {e}\nOutput:\n{result.stdout}")
        self.assertEqual(data, [], msg=f"Expected empty list, got: {data}")

    def test_search_invalid_source(self):
        """An unknown --source should produce a non-zero exit code."""
        result = run_downloader("--search", "Freeland", "--source", "badval")
        # No network call likely — error is raised before any HTTP request.
        self.assertNotEqual(result.returncode, 0,
                            msg=f"Expected failure for invalid source\nOutput:\n{result.stdout}{result.stderr}")


# ---------------------------------------------------------------------------
# 2. TestInfo
# ---------------------------------------------------------------------------
class TestInfo(unittest.TestCase):
    """Tests for the --info flag."""

    def test_info_plain(self):
        """Plain info output should contain the LCCN and newspaper name."""
        result = run_downloader("--info", KNOWN_LCCN)
        skip_if_network_error(result)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn(KNOWN_LCCN, combined)
        self.assertIn(KNOWN_TITLE.lower(), combined.lower(),
                      msg=f"Expected '{KNOWN_TITLE}' (case-insensitive) in output:\n{combined}")

    def test_info_json(self):
        """JSON info should return a dict with required keys."""
        result = run_downloader("--info", KNOWN_LCCN, "--json")
        skip_if_network_error(result)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            self.fail(f"stdout is not valid JSON: {e}\nOutput:\n{result.stdout}")
        self.assertIsInstance(data, dict, "Expected a JSON object, not a list")
        for key in ("lccn", "title", "start_year", "end_year"):
            self.assertIn(key, data, msg=f"Missing key '{key}' in: {data}")
        self.assertIn(KNOWN_LCCN, data.get("lccn", "") or "")
        self.assertIn(KNOWN_TITLE.lower(), (data.get("title", "") or "").lower(),
                      msg=f"Unexpected title: {data.get('title')}")
        self.assertIsNotNone(data["start_year"], "start_year should not be None")
        self.assertIsNotNone(data["end_year"],   "end_year should not be None")

    def test_info_unknown_lccn(self):
        """A non-existent LCCN should exit 0 and say 'Could not find' (or '{}' in JSON)."""
        result = run_downloader("--info", "sn00000000")
        skip_if_network_error(result)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn("Could not find", combined,
                      msg=f"Expected 'Could not find' in output:\n{combined}")

    def test_info_unknown_lccn_json(self):
        """--info with unknown LCCN and --json should return '{}'."""
        result = run_downloader("--info", "sn00000000", "--json")
        skip_if_network_error(result)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("{}", result.stdout,
                      msg=f"Expected '{{}}' in stdout:\n{result.stdout}")


# ---------------------------------------------------------------------------
# 3. TestDownload
# ---------------------------------------------------------------------------
class TestDownload(unittest.TestCase):
    """Tests for the --lccn download mode."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="paperrouter_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _pdf_count(self, root):
        return list(Path(root).rglob("*.pdf"))

    def test_download_single_issue(self):
        """Downloading 1 issue should create at least one PDF file."""
        result = run_downloader(
            "--lccn", KNOWN_LCCN,
            "--max-issues", "1",
            "--output", self.tmpdir,
            "--speed", "standard",
            timeout=180
        )
        skip_if_network_error(result)
        self.assertEqual(result.returncode, 0,
                         msg=f"Downloader exited non-zero\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        pdfs = self._pdf_count(self.tmpdir)
        self.assertGreater(len(pdfs), 0,
                           msg=f"No PDF files created under {self.tmpdir}")

    def test_download_with_year_filter(self):
        """Year-filtered download should place files in a year-named subdirectory."""
        result = run_downloader(
            "--lccn", KNOWN_LCCN,
            "--years", "1895",
            "--max-issues", "1",
            "--output", self.tmpdir,
            "--speed", "standard",
            timeout=180
        )
        skip_if_network_error(result)
        self.assertEqual(result.returncode, 0,
                         msg=f"Downloader exited non-zero\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        # PDFs should be under a 1895/ subdirectory
        year_dir = Path(self.tmpdir) / "1895"
        pdfs = list(year_dir.rglob("*.pdf")) if year_dir.exists() else []
        self.assertGreater(len(pdfs), 0,
                           msg=f"No PDFs in {year_dir}. All files: {list(Path(self.tmpdir).rglob('*'))}")

    def test_download_invalid_year(self):
        """An invalid --years string should exit 1 and print an error."""
        result = run_downloader(
            "--lccn", KNOWN_LCCN,
            "--years", "not_a_year",
            "--output", self.tmpdir,
            timeout=30
        )
        self.assertEqual(result.returncode, 1,
                         msg=f"Expected exit 1 for invalid year\nOutput:\n{result.stdout}{result.stderr}")
        combined = result.stdout + result.stderr
        self.assertTrue(
            "error" in combined.lower() or "Error" in combined,
            msg=f"Expected error message in output:\n{combined}"
        )

    def test_download_metadata_json_created(self):
        """A successful download should create a download_metadata.json file."""
        result = run_downloader(
            "--lccn", KNOWN_LCCN,
            "--max-issues", "1",
            "--output", self.tmpdir,
            "--speed", "standard",
            timeout=180
        )
        skip_if_network_error(result)
        self.assertEqual(result.returncode, 0,
                         msg=f"Downloader exited non-zero\nstdout:\n{result.stdout}")
        meta_path = Path(self.tmpdir) / "download_metadata.json"
        self.assertTrue(meta_path.exists(),
                        msg=f"download_metadata.json not created in {self.tmpdir}")
        with open(meta_path) as f:
            meta = json.load(f)
        self.assertEqual(meta.get("lccn"), KNOWN_LCCN)
        self.assertIn("downloaded", meta)

    def test_download_resume_skip(self):
        """Re-running after a complete download should skip already-downloaded issues."""
        # First run
        result1 = run_downloader(
            "--lccn", KNOWN_LCCN,
            "--max-issues", "1",
            "--output", self.tmpdir,
            "--speed", "standard",
            timeout=180
        )
        skip_if_network_error(result1)
        self.assertEqual(result1.returncode, 0)

        # Second run — should report skipping (no new downloads)
        result2 = run_downloader(
            "--lccn", KNOWN_LCCN,
            "--max-issues", "1",
            "--output", self.tmpdir,
            "--speed", "standard",
            timeout=60
        )
        skip_if_network_error(result2)
        self.assertEqual(result2.returncode, 0)
        combined = result2.stdout + result2.stderr
        # Expect skip indication OR 0 downloaded pages (already on disk)
        has_skip = "skip" in combined.lower() or "Skipping" in combined
        self.assertTrue(has_skip,
                        msg=f"Expected skip indication on second run:\n{combined}")


# ---------------------------------------------------------------------------
# 4. TestHarness
# ---------------------------------------------------------------------------
class TestHarness(unittest.TestCase):
    """Tests for harness.py (process wrapper)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="paperrouter_harness_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Clean up any stale PID file the test may have left
        pid_file = PROJECT_ROOT / ".harness.pid"
        if pid_file.exists():
            pid_file.unlink(missing_ok=True)

    def test_harness_kill_no_pid(self):
        """harness.py --kill with no PID file should exit 0 and say 'nothing to kill'."""
        # Ensure no stale PID file exists
        pid_file = PROJECT_ROOT / ".harness.pid"
        pid_file.unlink(missing_ok=True)

        result = run_harness("--kill", timeout=15)
        self.assertEqual(result.returncode, 0,
                         msg=f"Expected exit 0\nOutput:\n{result.stdout}{result.stderr}")
        combined = result.stdout + result.stderr
        self.assertIn("nothing to kill", combined.lower(),
                      msg=f"Expected 'nothing to kill' in:\n{combined}")

    def test_harness_wraps_download(self):
        """harness.py should wrap downloader.py and exit 0 for a real single-issue download."""
        harness_log = PROJECT_ROOT / "harness.log"
        before_mtime = harness_log.stat().st_mtime if harness_log.exists() else 0

        result = run_harness(
            "--lccn", KNOWN_LCCN,
            "--max-issues", "1",
            "--output", self.tmpdir,
            "--speed", "standard",
            env_extra={
                "HARNESS_TIMEOUT": "30",       # Don't wait 120 min
                "HARNESS_MEM_MB": "8000",
            },
            timeout=240
        )
        skip_if_network_error(result)
        self.assertEqual(result.returncode, 0,
                         msg=f"Harness exited non-zero\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")

        # harness.log should have been written / updated
        self.assertTrue(harness_log.exists(), "harness.log was not created")
        after_mtime = harness_log.stat().st_mtime
        self.assertGreater(after_mtime, before_mtime,
                           msg="harness.log mtime was not updated — harness may not have run")

    def test_harness_no_args_prints_usage(self):
        """Running harness.py with no args (other than --kill) should print usage and exit 1."""
        result = run_harness(timeout=10)
        self.assertEqual(result.returncode, 1,
                         msg=f"Expected exit 1\nOutput:\n{result.stdout}{result.stderr}")
        combined = result.stdout + result.stderr
        self.assertTrue(
            "usage" in combined.lower() or "harness" in combined.lower(),
            msg=f"Expected usage text:\n{combined}"
        )


# ---------------------------------------------------------------------------
# 5. TestEdgeCases
# ---------------------------------------------------------------------------
class TestEdgeCases(unittest.TestCase):
    """Argument validation and edge-case tests."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="paperrouter_edge_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_args_prints_help(self):
        """Running downloader.py without args should exit 0 and show help text."""
        result = run_downloader(timeout=10)
        self.assertEqual(result.returncode, 0,
                         msg=f"Expected exit 0 (help)\nOutput:\n{result.stdout}{result.stderr}")
        combined = result.stdout + result.stderr
        self.assertTrue(
            "usage" in combined.lower() or "--lccn" in combined,
            msg=f"Expected help/usage text:\n{combined}"
        )

    def test_ocr_batch_no_downloads(self):
        """--ocr-batch with no prior downloads should exit 0 with a warning."""
        result = run_downloader(
            "--lccn", KNOWN_LCCN,
            "--ocr-batch",
            "--output", self.tmpdir,
            timeout=30
        )
        self.assertEqual(result.returncode, 0,
                         msg=f"Unexpected exit code\nOutput:\n{result.stdout}{result.stderr}")
        combined = result.stdout + result.stderr
        # Should warn that there's nothing to OCR
        self.assertTrue(
            "no downloaded" in combined.lower() or "warning" in combined.lower(),
            msg=f"Expected a 'no downloaded issues' warning:\n{combined}"
        )

    def test_invalid_lccn_format_fetches_empty(self):
        """An LCCN that doesn't exist in LOC should return 'No issues found', not crash."""
        result = run_downloader(
            "--lccn", "invalidlccn",
            "--max-issues", "1",
            "--output", self.tmpdir,
            timeout=60
        )
        skip_if_network_error(result)
        self.assertEqual(result.returncode, 0,
                         msg=f"Expected exit 0 (no issues found)\nOutput:\n{result.stdout}{result.stderr}")
        combined = result.stdout + result.stderr
        self.assertIn("No issues found", combined,
                      msg=f"Expected 'No issues found':\n{combined}")

    def test_speed_standard_accepted(self):
        """--speed standard should be accepted by argparse without error."""
        # Just fetch issued list then bail — we're testing argument parsing only
        result = run_downloader(
            "--lccn", KNOWN_LCCN,
            "--max-issues", "1",
            "--speed", "standard",
            "--output", self.tmpdir,
            timeout=180
        )
        skip_if_network_error(result)
        # We don't check returncode here (download may or may not succeed);
        # the key thing is that 'invalid choice' is NOT in the output.
        combined = result.stdout + result.stderr
        self.assertNotIn("invalid choice", combined,
                         msg="'--speed standard' was not accepted by argparse")

    def test_speed_safe_accepted(self):
        """--speed safe should be accepted by argparse without error."""
        # Dry-run: just check the argument is accepted (fetch issues but limit immediately)
        result = run_downloader(
            "--lccn", KNOWN_LCCN,
            "--max-issues", "1",
            "--speed", "safe",
            "--output", self.tmpdir,
            timeout=180
        )
        skip_if_network_error(result)
        combined = result.stdout + result.stderr
        self.assertNotIn("invalid choice", combined,
                         msg="'--speed safe' was not accepted by argparse")

    def test_ocr_choice_none_accepted(self):
        """--ocr none should be accepted by argparse without error."""
        result = run_downloader(
            "--lccn", KNOWN_LCCN,
            "--max-issues", "1",
            "--ocr", "none",
            "--output", self.tmpdir,
            timeout=180
        )
        skip_if_network_error(result)
        combined = result.stdout + result.stderr
        self.assertNotIn("invalid choice", combined)

    def test_ocr_choice_loc_accepted(self):
        """--ocr loc should be accepted by argparse without error."""
        result = run_downloader(
            "--lccn", KNOWN_LCCN,
            "--max-issues", "1",
            "--ocr", "loc",
            "--output", self.tmpdir,
            timeout=180
        )
        skip_if_network_error(result)
        combined = result.stdout + result.stderr
        self.assertNotIn("invalid choice", combined)

    def test_force_ocr_accepted(self):
        """--force-ocr should be accepted by argparse without error."""
        result = run_downloader(
            "--lccn", KNOWN_LCCN,
            "--ocr-batch",
            "--force-ocr",
            "--ocr", "loc",
            "--output", self.tmpdir,
            timeout=30
        )
        combined = result.stdout + result.stderr
        self.assertNotIn("unrecognized arguments", combined,
                         msg="'--force-ocr' was not accepted by argparse")

    def test_date_flag_accepted(self):
        """--date should be accepted by argparse without error."""
        result = run_downloader(
            "--lccn", KNOWN_LCCN,
            "--ocr-batch",
            "--date", "1900-01-04",
            "--ocr", "loc",
            "--output", self.tmpdir,
            timeout=30
        )
        combined = result.stdout + result.stderr
        self.assertNotIn("unrecognized arguments", combined,
                         msg="'--date' was not accepted by argparse")


# ---------------------------------------------------------------------------
# 6. TestParseYearRange — pure unit tests (no network)
# ---------------------------------------------------------------------------
class TestParseYearRange(unittest.TestCase):
    """Unit tests for the parse_year_range() helper in downloader.py."""

    @classmethod
    def setUpClass(cls):
        # Import the helper directly for fast unit testing
        sys.path.insert(0, str(PROJECT_ROOT))
        from downloader import parse_year_range
        cls.parse = staticmethod(parse_year_range)

    def test_single_year(self):
        self.assertEqual(self.parse("1895"), [1895])

    def test_year_range(self):
        self.assertEqual(self.parse("1893-1895"), [1893, 1894, 1895])

    def test_mixed_list(self):
        self.assertEqual(self.parse("1880,1893-1895"), [1880, 1893, 1894, 1895])

    def test_multiple_disjoint_ranges(self):
        self.assertEqual(self.parse("1880-1882,1890-1891"), [1880, 1881, 1882, 1890, 1891])

    def test_duplicate_years_deduped(self):
        result = self.parse("1895,1895-1896")
        self.assertEqual(len(result), len(set(result)), "Duplicates should be removed")
        self.assertIn(1895, result)
        self.assertIn(1896, result)

    def test_whitespace_tolerance(self):
        """Parts with surrounding spaces should parse correctly."""
        self.assertEqual(self.parse("1895 , 1900"), [1895, 1900])

    def test_invalid_string_raises(self):
        with self.assertRaises((ValueError, TypeError)):
            self.parse("abcd")

    def test_result_is_sorted(self):
        result = self.parse("1900,1880,1890")
        self.assertEqual(result, sorted(result))


# ---------------------------------------------------------------------------
# 7. TestValidateLCCN — pure unit tests (no network)
# ---------------------------------------------------------------------------
class TestValidateLCCN(unittest.TestCase):
    """Unit tests for the validate_lccn() helper in downloader.py."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(PROJECT_ROOT))
        from downloader import validate_lccn
        cls.validate = staticmethod(validate_lccn)

    def test_valid_sn_lccn(self):
        self.assertTrue(self.validate("sn87080287"))

    def test_valid_longer_lccn(self):
        # Some LCCNs use other prefixes or longer digit strings
        self.assertTrue(self.validate("sn2001052366"))

    def test_invalid_too_short(self):
        self.assertFalse(self.validate("sn123"))

    def test_invalid_no_alpha_prefix(self):
        self.assertFalse(self.validate("1234567890"))

    def test_invalid_empty_string(self):
        self.assertFalse(self.validate(""))

    def test_invalid_uppercase(self):
        # Pattern requires lowercase alpha prefix
        self.assertFalse(self.validate("SN87080287"))

    def test_invalid_with_hyphen(self):
        self.assertFalse(self.validate("sn-87080287"))

    def test_invalid_with_spaces(self):
        self.assertFalse(self.validate("sn 87080287"))


# ---------------------------------------------------------------------------
# 8. TestMetadataEndpoint — unit tests for /api/metadata (no network)
# ---------------------------------------------------------------------------
class TestMetadataEndpoint(unittest.TestCase):
    """Unit tests for the /api/metadata Flask endpoint."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(PROJECT_ROOT))
        from web_gui import app
        cls.app = app
        cls.client = app.test_client()

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="paperrouter_meta_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_metadata(self, data):
        meta_path = Path(self.tmpdir) / "download_metadata.json"
        with open(meta_path, 'w') as f:
            json.dump(data, f)

    def test_no_metadata_file(self):
        """Should return found=False when no metadata file exists."""
        resp = self.client.get(f"/api/metadata?output={self.tmpdir}")
        data = resp.get_json()
        self.assertFalse(data["found"])

    def test_valid_metadata(self):
        """Should return year summary from a valid metadata file."""
        self._write_metadata({
            "lccn": "sn87080287",
            "newspaper_title": "Freeland tribune.",
            "downloaded": {
                "1900-01-04_ed-1": {
                    "date": "1900-01-04",
                    "edition": 1,
                    "complete": True,
                    "pages": [
                        {"page": 1, "file": "1900/page01.pdf", "size": 100},
                        {"page": 2, "file": "1900/page02.pdf", "size": 200}
                    ]
                }
            },
            "failed": {},
            "failed_pages": {}
        })
        resp = self.client.get(f"/api/metadata?output={self.tmpdir}")
        data = resp.get_json()
        self.assertTrue(data["found"])
        self.assertEqual(data["lccn"], "sn87080287")
        self.assertEqual(data["title"], "Freeland tribune.")
        self.assertEqual(data["total_issues"], 1)
        self.assertEqual(data["total_pages"], 2)
        self.assertIn("1900", data["years"])
        self.assertEqual(data["years"]["1900"]["issues"], 1)
        self.assertEqual(data["years"]["1900"]["pages"], 2)

    def test_empty_downloaded(self):
        """Should return found=True but zero totals when downloaded is empty."""
        self._write_metadata({
            "lccn": "sn87080287",
            "newspaper_title": "Test",
            "downloaded": {},
            "failed": {}
        })
        resp = self.client.get(f"/api/metadata?output={self.tmpdir}")
        data = resp.get_json()
        self.assertTrue(data["found"])
        self.assertEqual(data["total_issues"], 0)
        self.assertEqual(data["total_pages"], 0)


# ===========================================================================
# Updater Tests
# ===========================================================================

class TestParseVersion(unittest.TestCase):
    """Unit tests for updater.parse_version()."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(PROJECT_ROOT))
        from updater import parse_version
        cls._parse = staticmethod(parse_version)

    def pv(self, v):
        return self._parse(v)

    def test_simple_version(self):
        self.assertEqual(self.pv("0.2.0")[:3], (0, 2, 0))

    def test_v_prefix_stripped(self):
        self.assertEqual(self.pv("v0.3.0")[:3], (0, 3, 0))

    def test_prerelease_sorts_before_release(self):
        """0.2.0-alpha should be less than 0.2.0."""
        self.assertLess(self.pv("0.2.0-alpha"), self.pv("0.2.0"))

    def test_higher_version_wins(self):
        """0.3.0 should be greater than 0.2.0-alpha."""
        self.assertGreater(self.pv("v0.3.0"), self.pv("0.2.0-alpha"))

    def test_same_version_equal(self):
        self.assertEqual(self.pv("0.2.0"), self.pv("v0.2.0"))

    def test_patch_increment(self):
        self.assertGreater(self.pv("0.2.1"), self.pv("0.2.0"))

    def test_two_part_version_padded(self):
        """A version like '1.0' should be padded to (1, 0, 0)."""
        self.assertEqual(self.pv("1.0")[:3], (1, 0, 0))


class TestUpdaterCLI(unittest.TestCase):
    """Test the updater.py CLI interface."""

    def test_check_only_json(self):
        """--check-only --json should return valid JSON and exit 0."""
        result = subprocess.run(
            [PYTHON, str(PROJECT_ROOT / "updater.py"), "--check-only", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout.strip())
        # Should have either update_available key or full update info
        self.assertIsInstance(data, dict)

    def test_check_only_plain(self):
        """--check-only should exit 0 and print human-readable output."""
        result = subprocess.run(
            [PYTHON, str(PROJECT_ROOT / "updater.py"), "--check-only"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(
            "up to date" in result.stdout.lower() or
            "update available" in result.stdout.lower()
        )


class TestGetLocalVersion(unittest.TestCase):
    """Test get_local_version() reads the VERSION file."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(PROJECT_ROOT))
        from updater import get_local_version
        cls._get_ver = staticmethod(get_local_version)

    def test_returns_string(self):
        ver = self._get_ver()
        self.assertIsInstance(ver, str)
        self.assertTrue(len(ver) > 0)

    def test_matches_version_file(self):
        expected = (PROJECT_ROOT / "VERSION").read_text().strip()
        self.assertEqual(self._get_ver(), expected)


class TestUpdateEndpoints(unittest.TestCase):
    """Test the /api/update/* and /api/version Flask endpoints."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(PROJECT_ROOT))
        from web_gui import app
        cls.client = app.test_client()

    def test_version_endpoint(self):
        """GET /api/version should return the current version string."""
        resp = self.client.get("/api/version")
        data = resp.get_json()
        self.assertIn("version", data)
        self.assertIsInstance(data["version"], str)
        self.assertTrue(len(data["version"]) > 0)

    def test_update_check_endpoint(self):
        """GET /api/update/check should return JSON (may or may not have an update)."""
        resp = self.client.get("/api/update/check")
        data = resp.get_json()
        self.assertIsInstance(data, dict)
        # Should always have update_available key (either from updater or error fallback)
        # The error case returns {"update_available": False, "error": "..."}
        # so just verify we got valid JSON back


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    unittest.main()
