"""
test_changes.py — Unit tests for the three change areas in this release.

All tests are pure unit tests (no network, no live API calls).
Each test class targets one specific change:

  1. TestProcessOcrForIssue   — _process_ocr_for_issue() helper
  2. TestRunSkipLogic         — run() skip / OCR-resume gate
  3. TestMetadataSaveOrder    — PDF metadata saved before OCR starts
  4. TestOcrFailureLogging    — process_issue_batch() logs LOC OCR failures
  5. TestRunOcrBatch          — run_ocr_batch() delegates to _process_ocr_for_issue
  6. TestCheckDependencies    — gui._check_and_install_dependencies()

Run from the project root:
    python -m unittest tests/test_changes.py -v
"""

import importlib
import json
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_manager(tmpdir, ocr_mode="none", force_ocr=False, years=None):
    """
    Instantiate a DownloadManager pointing at *tmpdir* with a fully mocked
    source, so no network calls are made.  The returned manager has:
      manager.source        — MagicMock with sensible defaults
      manager.ocr_manager   — real OCRManager (but its sub-engines are mocks)
    """
    with patch("downloader.get_source") as mock_get_source:
        mock_source = MagicMock()
        mock_source.display_name = "Test Source"
        mock_source.build_page_url.return_value = "https://www.loc.gov/resource/test/"
        mock_get_source.return_value = mock_source

        from downloader import DownloadManager

        manager = DownloadManager(
            lccn="sn87080287",
            output_dir=str(tmpdir),
            source_name="loc",
            ocr_mode=ocr_mode,
            force_ocr=force_ocr,
            years=years,
        )
    # set_download_delay to 0 so run() never sleeps in tests
    manager.download_delay = 0
    manager._last_download_time = 0
    return manager


def _issue_info(date="1900-01-04", edition=1, pages=None, ocr_complete=None):
    """Build a metadata dict for a downloaded issue."""
    info = {
        "date": date,
        "edition": edition,
        "complete": True,
        "pages": pages or [
            {"page": 1, "file": f"1900/sn87080287_{date}_ed-{edition}_page01.pdf", "size": 1000},
            {"page": 2, "file": f"1900/sn87080287_{date}_ed-{edition}_page02.pdf", "size": 1000},
        ],
    }
    if ocr_complete is not None:
        info["ocr_complete"] = ocr_complete
    return info


# ---------------------------------------------------------------------------
# 1. TestProcessOcrForIssue
# ---------------------------------------------------------------------------

class TestProcessOcrForIssue(unittest.TestCase):
    """Unit tests for DownloadManager._process_ocr_for_issue()."""

    DATE = "1900-01-04"
    EDITION = 1
    ISSUE_ID = "1900-01-04_ed-1"

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="pr_ocr_"))
        self.year_dir = self.tmpdir / "1900"
        self.year_dir.mkdir()

        self.manager = _make_manager(self.tmpdir, ocr_mode="loc")
        # Replace ocr_manager with a mock so we can track calls
        self.manager.ocr_manager = MagicMock()

        self.info = _issue_info(self.DATE, self.EDITION)
        self.manager.metadata["downloaded"][self.ISSUE_ID] = self.info

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- helpers ----

    def _loc(self, n):
        return self.year_dir / f"{self.DATE}_ed-{self.EDITION}_page{n:02d}_loc.txt"

    def _surya(self, n):
        return self.year_dir / f"{self.DATE}_ed-{self.EDITION}_page{n:02d}_surya.txt"

    # ---- tests ----

    def test_all_files_exist_nothing_processed(self):
        """All OCR files present → process_page never called."""
        self._loc(1).touch()
        self._loc(2).touch()
        self.manager._process_ocr_for_issue(self.ISSUE_ID, self.info)
        self.manager.ocr_manager.process_page.assert_not_called()

    def test_all_files_exist_sets_ocr_complete(self):
        """All OCR files present → ocr_complete set True."""
        self._loc(1).touch()
        self._loc(2).touch()
        self.manager._process_ocr_for_issue(self.ISSUE_ID, self.info)
        self.assertTrue(self.info.get("ocr_complete"))

    def test_missing_one_file_processes_only_that_page(self):
        """Only the page missing its OCR file should be processed."""
        self._loc(1).touch()   # page 1 exists
        # page 2 is absent
        self.manager._process_ocr_for_issue(self.ISSUE_ID, self.info)
        self.assertEqual(self.manager.ocr_manager.process_page.call_count, 1)

    def test_no_files_processes_all_pages(self):
        """No OCR files → every page processed."""
        self.manager._process_ocr_for_issue(self.ISSUE_ID, self.info)
        self.assertEqual(self.manager.ocr_manager.process_page.call_count, 2)

    def test_ocr_complete_not_set_when_page_fails(self):
        """If process_page doesn't create the output file, ocr_complete stays unset."""
        # process_page is a mock — it won't create any files
        self.manager._process_ocr_for_issue(self.ISSUE_ID, self.info)
        self.assertFalse(self.info.get("ocr_complete", False))

    def test_force_ocr_clears_existing_ocr_complete(self):
        """force_ocr=True must clear ocr_complete before running."""
        self.info["ocr_complete"] = True
        self._loc(1).touch()
        self._loc(2).touch()
        self.manager.force_ocr = True

        self.manager._process_ocr_for_issue(self.ISSUE_ID, self.info)

        # All pages re-processed (skip check is bypassed by force_ocr)
        self.assertEqual(self.manager.ocr_manager.process_page.call_count, 2)

    def test_force_ocr_does_not_leave_stale_complete_when_files_absent(self):
        """force_ocr=True with no output files → ocr_complete NOT set."""
        self.info["ocr_complete"] = True
        self.manager.force_ocr = True

        self.manager._process_ocr_for_issue(self.ISSUE_ID, self.info)

        self.assertNotIn("ocr_complete", self.info)

    def test_empty_pages_list_no_crash(self):
        """An issue with an empty pages list should complete without error."""
        empty_info = {"date": self.DATE, "edition": 1, "complete": True, "pages": []}
        # Should not raise
        self.manager._process_ocr_for_issue(self.ISSUE_ID, empty_info)
        self.assertFalse(empty_info.get("ocr_complete", False))

    def test_surya_mode_checks_surya_files(self):
        """With ocr_mode='surya', skip logic checks _surya.txt files."""
        self.manager.ocr_mode = "surya"
        self._surya(1).touch()
        self._surya(2).touch()

        self.manager._process_ocr_for_issue(self.ISSUE_ID, self.info)

        self.manager.ocr_manager.process_page.assert_not_called()
        self.assertTrue(self.info.get("ocr_complete"))

    def test_both_mode_requires_both_files_to_skip_page(self):
        """ocr_mode='both': a page is only skipped when BOTH its files exist."""
        self.manager.ocr_mode = "both"
        # Only LOC files present; Surya files absent
        self._loc(1).touch()
        self._loc(2).touch()

        self.manager._process_ocr_for_issue(self.ISSUE_ID, self.info)

        # Both pages must still be processed (Surya files missing)
        self.assertEqual(self.manager.ocr_manager.process_page.call_count, 2)

    def test_both_mode_skips_when_both_files_exist(self):
        """ocr_mode='both': skip page only when both _loc and _surya files exist."""
        self.manager.ocr_mode = "both"
        self._loc(1).touch()
        self._loc(2).touch()
        self._surya(1).touch()
        self._surya(2).touch()

        self.manager._process_ocr_for_issue(self.ISSUE_ID, self.info)

        self.manager.ocr_manager.process_page.assert_not_called()
        self.assertTrue(self.info.get("ocr_complete"))

    def test_process_page_called_with_correct_page_num(self):
        """process_page must be called with the correct page numbers."""
        self.manager._process_ocr_for_issue(self.ISSUE_ID, self.info)

        call_args_list = self.manager.ocr_manager.process_page.call_args_list
        page_nums = [c.args[0].page_num for c in call_args_list]
        self.assertIn(1, page_nums)
        self.assertIn(2, page_nums)


# ---------------------------------------------------------------------------
# 2. TestRunSkipLogic
# ---------------------------------------------------------------------------

class TestRunSkipLogic(unittest.TestCase):
    """
    Unit tests for the skip / OCR-resume gate at the top of DownloadManager.run().
    """

    DATE = "1900-01-04"
    EDITION = 1
    ISSUE_ID = "1900-01-04_ed-1"

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="pr_skip_"))

        from sources.base import IssueMetadata
        self.mock_issue = IssueMetadata(
            date=self.DATE,
            edition=self.EDITION,
            url="https://www.loc.gov/resource/test/",
            year=1900,
            lccn="sn87080287",
            title="Freeland Tribune",
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _manager(self, ocr_mode="none", force_ocr=False, ocr_complete=None):
        m = _make_manager(self.tmpdir, ocr_mode=ocr_mode, force_ocr=force_ocr)
        info = _issue_info(self.DATE, self.EDITION, ocr_complete=ocr_complete)
        m.metadata["downloaded"][self.ISSUE_ID] = info
        m._fetch_newspaper_issues = MagicMock(return_value=[self.mock_issue])
        m._process_ocr_for_issue = MagicMock()
        m._save_metadata = MagicMock()
        return m

    # ---- ocr_mode=none ----

    def test_ocr_none_fully_skips(self):
        """`ocr_mode='none'`: downloaded issue should be skipped entirely."""
        m = self._manager(ocr_mode="none")
        m.run()
        m._process_ocr_for_issue.assert_not_called()
        self.assertEqual(m.stats["skipped"], 1)
        self.assertEqual(m.stats["downloaded"], 0)

    # ---- ocr_complete already True ----

    def test_ocr_complete_true_fully_skips(self):
        """`ocr_complete=True`: issue already finished, skip without re-running OCR."""
        m = self._manager(ocr_mode="loc", ocr_complete=True)
        m.run()
        m._process_ocr_for_issue.assert_not_called()
        self.assertEqual(m.stats["skipped"], 1)

    # ---- OCR needed (resume path) ----

    def test_ocr_needed_resumes_ocr(self):
        """Downloaded but no `ocr_complete`: must call _process_ocr_for_issue."""
        m = self._manager(ocr_mode="loc", ocr_complete=None)
        m.run()
        m._process_ocr_for_issue.assert_called_once()

    def test_ocr_resume_passes_correct_issue_id(self):
        """OCR-resume call must use the correct issue_id."""
        m = self._manager(ocr_mode="loc", ocr_complete=None)
        m.run()
        first_positional_arg = m._process_ocr_for_issue.call_args.args[0]
        self.assertEqual(first_positional_arg, self.ISSUE_ID)

    def test_ocr_resume_counts_as_skipped_not_downloaded(self):
        """OCR-only resume should increment skipped, not downloaded."""
        m = self._manager(ocr_mode="loc", ocr_complete=None)
        m.run()
        self.assertEqual(m.stats["skipped"], 1)
        self.assertEqual(m.stats["downloaded"], 0)

    def test_ocr_resume_saves_metadata(self):
        """OCR-resume path must call _save_metadata after _process_ocr_for_issue."""
        m = self._manager(ocr_mode="loc", ocr_complete=None)
        m.run()
        m._save_metadata.assert_called()

    # ---- force_ocr bypasses skip gate ----

    def test_force_ocr_bypasses_skip_gate(self):
        """`force_ocr=True` should bypass the skip gate and hit the download path."""
        m = self._manager(ocr_mode="loc", force_ocr=True, ocr_complete=True)
        m.source.get_pages_for_issue.return_value = []  # nothing to download
        m.run()
        # If the skip gate were hit, get_pages_for_issue would never be called
        m.source.get_pages_for_issue.assert_called_once()

    # ---- retry_failed bypasses skip gate ----

    def test_retry_failed_bypasses_skip_gate(self):
        """`retry_failed=True` should bypass the skip gate."""
        m = self._manager(ocr_mode="none")
        m.retry_failed = True
        m.source.get_pages_for_issue.return_value = []
        m.run()
        m.source.get_pages_for_issue.assert_called_once()


# ---------------------------------------------------------------------------
# 3. TestMetadataSaveOrder
# ---------------------------------------------------------------------------

class TestMetadataSaveOrder(unittest.TestCase):
    """
    Verify that PDF-completion metadata is saved BEFORE OCR starts.
    Previously, metadata was saved only after OCR finished, meaning a crash
    during OCR lost the PDF completion record.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="pr_order_"))
        self.year_dir = self.tmpdir / "1900"
        self.year_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_metadata_saved_before_ocr_starts(self):
        from sources.base import IssueMetadata, PageMetadata, DownloadResult

        manager = _make_manager(self.tmpdir, ocr_mode="loc")

        mock_issue = IssueMetadata(
            date="1900-01-04", edition=1,
            url="https://www.loc.gov/resource/test/",
            year=1900, lccn="sn87080287", title="Freeland Tribune",
        )
        mock_page = PageMetadata(
            issue_date="1900-01-04", edition=1, page_num=1,
            url="https://www.loc.gov/resource/test/p1/", lccn="sn87080287",
        )

        manager._fetch_newspaper_issues = MagicMock(return_value=[mock_issue])
        manager.source.get_pages_for_issue.return_value = [mock_page]
        manager.source.download_page_pdf.return_value = DownloadResult(
            success=True, size_bytes=0
        )

        call_order = []
        manager._save_metadata = MagicMock(
            side_effect=lambda: call_order.append("save")
        )
        manager._process_ocr_for_issue = MagicMock(
            side_effect=lambda *a, **kw: call_order.append("ocr")
        )

        manager.run()

        self.assertIn("save", call_order, "_save_metadata was never called")
        self.assertIn("ocr", call_order, "_process_ocr_for_issue was never called")

        first_save = next(i for i, x in enumerate(call_order) if x == "save")
        first_ocr = next(i for i, x in enumerate(call_order) if x == "ocr")
        self.assertLess(
            first_save, first_ocr,
            f"Expected 'save' before 'ocr' but got: {call_order}",
        )

    def test_partial_download_still_saves_failed_metadata(self):
        """If PDFs only partially succeed, the failure is saved to metadata."""
        from sources.base import IssueMetadata, PageMetadata, DownloadResult

        manager = _make_manager(self.tmpdir, ocr_mode="loc")

        mock_issue = IssueMetadata(
            date="1900-01-04", edition=1,
            url="https://www.loc.gov/resource/test/",
            year=1900, lccn="sn87080287", title="Freeland Tribune",
        )
        # Two pages
        pages = [
            PageMetadata(issue_date="1900-01-04", edition=1, page_num=i,
                         url=f"https://loc.gov/test/{i}/", lccn="sn87080287")
            for i in (1, 2)
        ]

        manager._fetch_newspaper_issues = MagicMock(return_value=[mock_issue])
        manager.source.get_pages_for_issue.return_value = pages
        # Page 1 succeeds, page 2 fails
        manager.source.download_page_pdf.side_effect = [
            DownloadResult(success=True, size_bytes=0),
            DownloadResult(success=False, error="HTTP 503"),
        ]

        manager._process_ocr_for_issue = MagicMock()
        manager._save_metadata = MagicMock()

        manager.run()

        # OCR must NOT have been called (partial download)
        manager._process_ocr_for_issue.assert_not_called()
        # The failed issue must be recorded in metadata
        self.assertIn("1900-01-04_ed-1", manager.metadata.get("failed", {}))


# ---------------------------------------------------------------------------
# 4. TestOcrFailureLogging
# ---------------------------------------------------------------------------

class TestOcrFailureLogging(unittest.TestCase):
    """
    Verify that LOC OCR failures are surfaced as WARNING log messages.
    Previously the else-branch was missing, silencing all failures.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="pr_log_"))
        (self.tmpdir / "1900").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_ocr_manager(self):
        from ocr_engine import OCRManager
        mock_logger = MagicMock(spec=logging.Logger)
        return OCRManager(self.tmpdir, mock_logger), mock_logger

    def _page(self):
        from sources.base import PageMetadata
        return PageMetadata(
            issue_date="1900-01-04", edition=1, page_num=1,
            url="https://www.loc.gov/resource/test/", lccn="sn87080287",
        )

    def test_loc_failure_logged_as_warning(self):
        """A failed fetch_ocr_text call must trigger logger.warning."""
        from sources.base import OCRResult
        mgr, mock_logger = self._make_ocr_manager()

        mock_source = MagicMock()
        mock_source.fetch_ocr_text.return_value = OCRResult(
            success=False, error="No OCR service found"
        )

        mgr.process_issue_batch([self._page()], mock_source, "loc", [])

        mock_logger.warning.assert_called_once()
        warning_text = str(mock_logger.warning.call_args)
        self.assertIn("No OCR service found", warning_text)

    def test_loc_success_does_not_warn(self):
        """A successful fetch_ocr_text call must NOT trigger a warning."""
        from sources.base import OCRResult
        mgr, mock_logger = self._make_ocr_manager()

        mock_source = MagicMock()
        mock_source.fetch_ocr_text.return_value = OCRResult(success=True, word_count=150)

        mgr.process_issue_batch([self._page()], mock_source, "loc", [])

        mock_logger.warning.assert_not_called()

    def test_failure_message_contains_page_number(self):
        """The warning must include the failing page number."""
        from sources.base import OCRResult
        mgr, mock_logger = self._make_ocr_manager()

        mock_source = MagicMock()
        mock_source.fetch_ocr_text.return_value = OCRResult(
            success=False, error="timeout"
        )

        mgr.process_issue_batch([self._page()], mock_source, "loc", [])

        warning_text = str(mock_logger.warning.call_args)
        self.assertIn("1", warning_text)   # page_num = 1

    def test_multiple_failures_each_logged(self):
        """Each failing page should produce its own warning."""
        from sources.base import OCRResult, PageMetadata
        mgr, mock_logger = self._make_ocr_manager()

        pages = [
            PageMetadata(issue_date="1900-01-04", edition=1, page_num=i,
                         url=f"https://loc.gov/test/{i}/", lccn="sn87080287")
            for i in (1, 2, 3)
        ]

        mock_source = MagicMock()
        mock_source.fetch_ocr_text.return_value = OCRResult(
            success=False, error="server error"
        )

        mgr.process_issue_batch(pages, mock_source, "loc", [])

        self.assertEqual(mock_logger.warning.call_count, 3)


# ---------------------------------------------------------------------------
# 5. TestRunOcrBatch
# ---------------------------------------------------------------------------

class TestRunOcrBatch(unittest.TestCase):
    """
    Verify run_ocr_batch() delegates per-issue work to _process_ocr_for_issue
    and saves metadata after each issue.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="pr_batch_"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _manager_with_issues(self, issues_dict, ocr_mode="loc", years=None):
        m = _make_manager(self.tmpdir, ocr_mode=ocr_mode, years=years)
        m.metadata["downloaded"] = issues_dict
        m._process_ocr_for_issue = MagicMock()
        m._save_metadata = MagicMock()
        return m

    # ---- delegation ----

    def test_delegates_to_process_ocr_for_issue(self):
        """Each eligible issue should produce exactly one _process_ocr_for_issue call."""
        issues = {
            "1900-01-04_ed-1": _issue_info("1900-01-04"),
            "1900-01-11_ed-1": _issue_info("1900-01-11"),
        }
        m = self._manager_with_issues(issues)
        m.run_ocr_batch()
        self.assertEqual(m._process_ocr_for_issue.call_count, 2)

    def test_correct_issue_ids_passed(self):
        """_process_ocr_for_issue must receive the correct issue_id strings."""
        issues = {
            "1900-01-04_ed-1": _issue_info("1900-01-04"),
        }
        m = self._manager_with_issues(issues)
        m.run_ocr_batch()
        called_id = m._process_ocr_for_issue.call_args.args[0]
        self.assertEqual(called_id, "1900-01-04_ed-1")

    def test_saves_metadata_after_each_issue(self):
        """_save_metadata must be called once per issue processed."""
        issues = {
            "1900-01-04_ed-1": _issue_info("1900-01-04"),
            "1900-01-11_ed-1": _issue_info("1900-01-11"),
        }
        m = self._manager_with_issues(issues)
        m.run_ocr_batch()
        self.assertEqual(m._save_metadata.call_count, 2)

    # ---- empty / no-op cases ----

    def test_empty_downloaded_exits_early(self):
        """No downloaded issues → early return, _process_ocr_for_issue not called."""
        m = self._manager_with_issues({})
        m.run_ocr_batch()
        m._process_ocr_for_issue.assert_not_called()

    def test_issue_without_pages_skipped(self):
        """Issues that have no 'pages' entry should be excluded."""
        issues = {
            "1900-01-04_ed-1": {"date": "1900-01-04", "edition": 1, "complete": True},
        }
        m = self._manager_with_issues(issues)
        m.run_ocr_batch()
        m._process_ocr_for_issue.assert_not_called()

    # ---- filters ----

    def test_year_filter_excludes_wrong_years(self):
        """Issues outside year_set should not be processed."""
        issues = {
            "1900-01-04_ed-1": _issue_info("1900-01-04"),
            "1901-01-03_ed-1": _issue_info("1901-01-03"),
        }
        m = self._manager_with_issues(issues, years=[1901])
        m.run_ocr_batch()
        self.assertEqual(m._process_ocr_for_issue.call_count, 1)
        processed_id = m._process_ocr_for_issue.call_args.args[0]
        self.assertIn("1901", processed_id)

    def test_date_filter_targets_single_issue(self):
        """--date filter should process only the matching date."""
        issues = {
            "1900-01-04_ed-1": _issue_info("1900-01-04"),
            "1900-01-11_ed-1": _issue_info("1900-01-11"),
        }
        m = self._manager_with_issues(issues)
        m.ocr_date = "1900-01-04"
        m.run_ocr_batch()
        self.assertEqual(m._process_ocr_for_issue.call_count, 1)
        processed_id = m._process_ocr_for_issue.call_args.args[0]
        self.assertIn("1900-01-04", processed_id)


# ---------------------------------------------------------------------------
# 6. TestCheckDependencies
# ---------------------------------------------------------------------------

class TestCheckDependencies(unittest.TestCase):
    """
    Unit tests for gui._check_and_install_dependencies().

    We patch importlib.util.find_spec to control which packages appear
    "missing", and mock all tkinter interactions so no window is shown.
    """

    @classmethod
    def setUpClass(cls):
        import gui  # noqa — ensure module is importable
        cls.gui = gui

    def test_all_present_returns_true_immediately(self):
        """All packages importable → True with no dialog."""
        with patch("importlib.util.find_spec", return_value=MagicMock()):
            result = self.gui._check_and_install_dependencies()
        self.assertTrue(result)

    def test_all_present_no_tk_created(self):
        """No missing packages → tk.Tk() must never be instantiated."""
        with patch("importlib.util.find_spec", return_value=MagicMock()):
            with patch.object(self.gui.tk, "Tk") as mock_tk:
                self.gui._check_and_install_dependencies()
        mock_tk.assert_not_called()

    def test_missing_package_prompts_user(self):
        """A missing package → askyesno dialog is shown."""
        def fake_find_spec(name):
            return None if name == "psutil" else MagicMock()

        with patch("importlib.util.find_spec", side_effect=fake_find_spec):
            with patch.object(self.gui.tk, "Tk", return_value=MagicMock()):
                with patch.object(
                    self.gui.messagebox, "askyesno", return_value=False
                ) as mock_ask:
                    self.gui._check_and_install_dependencies()

        mock_ask.assert_called_once()

    def test_user_declines_returns_false(self):
        """User clicks No → function returns False."""
        def fake_find_spec(name):
            return None if name == "psutil" else MagicMock()

        with patch("importlib.util.find_spec", side_effect=fake_find_spec):
            with patch.object(self.gui.tk, "Tk", return_value=MagicMock()):
                with patch.object(
                    self.gui.messagebox, "askyesno", return_value=False
                ):
                    result = self.gui._check_and_install_dependencies()

        self.assertFalse(result)

    def test_missing_package_listed_in_dialog(self):
        """The dialog message must name the missing package spec."""
        def fake_find_spec(name):
            return None if name == "flask" else MagicMock()

        captured = {}

        def capture_askyesno(title, msg, **kw):
            captured["msg"] = msg
            return False

        with patch("importlib.util.find_spec", side_effect=fake_find_spec):
            with patch.object(self.gui.tk, "Tk", return_value=MagicMock()):
                with patch.object(
                    self.gui.messagebox, "askyesno", side_effect=capture_askyesno
                ):
                    self.gui._check_and_install_dependencies()

        self.assertIn("flask>=3.0.0", captured.get("msg", ""))

    def test_successful_install_returns_true(self):
        """If pip install succeeds, function returns True."""
        def fake_find_spec(name):
            return None if name == "requests" else MagicMock()

        mock_root = MagicMock()
        mock_toplevel = MagicMock()
        mock_root.return_value = mock_root
        mock_toplevel.return_value = mock_toplevel

        with patch("importlib.util.find_spec", side_effect=fake_find_spec):
            with patch.object(self.gui.tk, "Tk", return_value=mock_root):
                with patch.object(self.gui.tk, "Toplevel", return_value=mock_toplevel):
                    with patch.object(
                        self.gui.messagebox, "askyesno", return_value=True
                    ):
                        with patch("subprocess.check_call", return_value=0):
                            result = self.gui._check_and_install_dependencies()

        self.assertTrue(result)

    def test_failed_install_returns_false(self):
        """If pip install fails (CalledProcessError), function returns False."""
        import subprocess

        def fake_find_spec(name):
            return None if name == "requests" else MagicMock()

        mock_root = MagicMock()
        mock_toplevel = MagicMock()

        with patch("importlib.util.find_spec", side_effect=fake_find_spec):
            with patch.object(self.gui.tk, "Tk", return_value=mock_root):
                with patch.object(self.gui.tk, "Toplevel", return_value=mock_toplevel):
                    with patch.object(
                        self.gui.messagebox, "askyesno", return_value=True
                    ):
                        with patch(
                            "subprocess.check_call",
                            side_effect=subprocess.CalledProcessError(1, "pip"),
                        ):
                            with patch.object(
                                self.gui.messagebox, "showerror"
                            ) as mock_err:
                                result = self.gui._check_and_install_dependencies()

        self.assertFalse(result)
        mock_err.assert_called_once()

    def test_only_missing_packages_installed(self):
        """pip install must be called only with the packages that are missing."""
        import subprocess

        def fake_find_spec(name):
            # Only psutil is missing; requests and flask are present
            return None if name == "psutil" else MagicMock()

        mock_root = MagicMock()
        mock_toplevel = MagicMock()

        with patch("importlib.util.find_spec", side_effect=fake_find_spec):
            with patch.object(self.gui.tk, "Tk", return_value=mock_root):
                with patch.object(self.gui.tk, "Toplevel", return_value=mock_toplevel):
                    with patch.object(
                        self.gui.messagebox, "askyesno", return_value=True
                    ):
                        with patch("subprocess.check_call", return_value=0) as mock_pip:
                            self.gui._check_and_install_dependencies()

        # check_call args: [sys.executable, "-m", "pip", "install", "psutil>=5.9.0"]
        pip_args = mock_pip.call_args.args[0]
        # Only psutil should be in the install list
        self.assertIn("psutil>=5.9.0", pip_args)
        self.assertNotIn("requests>=2.31.0", pip_args)
        self.assertNotIn("flask>=3.0.0", pip_args)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    unittest.main(verbosity=2)
