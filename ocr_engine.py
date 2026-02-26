import logging
import time
from pathlib import Path
from typing import List, Dict, Optional, Union

try:
    import fitz  # PyMuPDF
    from PIL import Image
    from surya.ocr import Predictor
    from surya.model.detection.model import load_model as load_det_model, load_predictor as load_det_predictor
    from surya.model.recognition.model import load_model as load_rec_model, load_predictor as load_rec_predictor
    from surya.model.ordering.processor import load_processor
    from surya.model.ordering.model import load_model as load_order_model
    from surya.postprocessing.text import sort_text_lines
    SURYA_AVAILABLE = True
except ImportError:
    SURYA_AVAILABLE = False

from sources.base import PageMetadata, NewspaperSource


class SuryaOCREngine:
    """Local AI-powered OCR with layout analysis using Surya."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.foundation_predictor = None
        self.det_predictor = None
        self.rec_predictor = None
        self.layout_predictor = None

    def _load_models(self):
        """Lazy load Surya models on first use."""
        if self.foundation_predictor:
            return

        try:
            from surya.foundation import FoundationPredictor
            from surya.detection import DetectionPredictor
            from surya.recognition import RecognitionPredictor
            from surya.layout import LayoutPredictor
            
            self.logger.info("  Loading Surya AI models (this may take a minute on first run)...")
            
            self.foundation_predictor = FoundationPredictor()
            self.det_predictor = DetectionPredictor()
            self.rec_predictor = RecognitionPredictor(self.foundation_predictor)
            self.layout_predictor = LayoutPredictor(self.foundation_predictor)
            
            self.logger.info("    Surya models loaded successfully.")
        except ImportError as e:
            self.logger.error(f"Failed to import Surya: {e}")
            raise ImportError("surya-ocr and pymupdf are required for local OCR.")

    def process_pages(self, pages: List[PageMetadata], output_dir: Path, pdf_paths: List[Path]) -> List[Dict]:
        """Process multiple pages in a batch using Surya AI models."""
        if not pdf_paths:
            return []

        try:
            self._load_models()
            from surya.common.surya.schema import TaskNames
            
            self.logger.debug(f"Running batched Surya on {len(pdf_paths)} images")
            
            import gc
            chunk_size = 4
            results = []
            
            for i in range(0, len(pdf_paths), chunk_size):
                chunk_paths = pdf_paths[i:i + chunk_size]
                chunk_pages = pages[i:i + chunk_size]
                
                chunk_images = []
                for pdf_path in chunk_paths:
                    doc = fitz.open(str(pdf_path))
                    fitz_page = doc.load_page(0)
                    zoom = 1.5
                    mat = fitz.Matrix(zoom, zoom)
                    pix = fitz_page.get_pixmap(matrix=mat)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    chunk_images.append(img)
                    doc.close()

                # 1. Batch Layout & OCR
                self.logger.debug(f"  Processing chunk {i//chunk_size + 1} ({len(chunk_images)} images)")
                layout_predictions = self.layout_predictor(chunk_images)
                ocr_predictions = self.rec_predictor(
                    chunk_images, 
                    task_names=[TaskNames.ocr_with_boxes] * len(chunk_images), 
                    det_predictor=self.det_predictor
                )
                
                for img in chunk_images:
                    img.close()
                gc.collect()

                for j, (page, layout_result, ocr_result) in enumerate(zip(chunk_pages, layout_predictions, ocr_predictions)):
                    full_text = "\n".join([line.text for line in ocr_result.text_lines])
                    
                    filename = f"{page.issue_date}_ed-{page.edition}_page{page.page_num:02d}_surya.txt"
                    output_path = output_dir / filename
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    
                    header = (
                        f"# OCR Text — {page.lccn} — {page.issue_date}\n"
                        f"# Page: {page.page_num}\n"
                        f"# OCR Method: surya-ai (batched)\n"
                        f"# ---\n\n"
                    )
                    
                    with open(output_path, 'w', encoding='utf-8') as f:
                        f.write(header + full_text)

                    results.append({
                        'success': True,
                        'method': 'surya',
                        'text_file': filename,
                        'text_path': str(output_path),
                        'word_count': len(full_text.split())
                    })
            return results
        except Exception as e:
            self.logger.error(f"Batched Surya OCR failed: {e}")
            return [{'success': False, 'error': str(e)}] * len(pages)

    def process_page(self, page: PageMetadata, output_dir: Path, pdf_path: Optional[Path] = None) -> Dict:
        """Process a single page using Surya AI models (legacy single-page wrapper)."""
        res = self.process_pages([page], output_dir, [pdf_path] if pdf_path else [])
        return res[0] if res else {'success': False, 'error': 'No result'}

class OCRManager:
    """Orchestrates OCR processing across different engines using source abstractions."""
    
    def __init__(self, output_dir: Path, logger: Optional[logging.Logger] = None):
        self.output_dir = output_dir
        self.logger = logger or logging.getLogger(__name__)
        self.surya_engine = None

    def process_page(self, page: PageMetadata, source: NewspaperSource, mode: str, pdf_path: Optional[Path] = None):
        """Process a page using the selected OCR mode."""
        year_dir = self.output_dir / str(page.issue_date[:4])

        if mode in ('loc', 'both'):
            res = source.fetch_ocr_text(page, year_dir)
            if res.success:
                self.logger.info(f"  Tier 1 OCR (Source): Success, {res.word_count} words")
            else:
                self.logger.warning(f"  Tier 1 OCR (Source): Failed: {res.error}")

        if mode in ('surya', 'both'):
            if not SURYA_AVAILABLE:
                self.logger.error(f"  Tier 2 OCR (Surya): Unavailable - surya-ocr not installed")
                return

            if not self.surya_engine:
                self.surya_engine = SuryaOCREngine(self.logger)

            if pdf_path and pdf_path.exists():
                res = self.surya_engine.process_page(page, year_dir, pdf_path)
                if res.get('success'):
                    self.logger.info(f"  Tier 2 OCR (Surya): Success, {res['word_count']} words")
                else:
                    self.logger.error(f"  Tier 2 OCR (Surya): Failed: {res.get('error')}")
            else:
                self.logger.warning(f"  Tier 2 OCR (Surya): Skipped - PDF not found at {pdf_path}")

    def process_issue_batch(self, pages: List[PageMetadata], source: NewspaperSource, mode: str, pdf_paths: List[Path]):
        """Process all pages of an issue as a batch where possible."""
        year_dir = self.output_dir / str(pages[0].issue_date[:4])
        
        # Tier 1 typically sequential due to API per-page nature, but we can parallelize if source supports it
        if mode in ('loc', 'both'):
            for page in pages:
                res = source.fetch_ocr_text(page, year_dir)
                if res.success:
                    self.logger.info(f"  Page {page.page_num} - Tier 1 OCR: Success, {res.word_count} words")

        # Tier 2 (Surya) is where batching really helps GPU throughput
        if mode in ('surya', 'both'):
            if not self.surya_engine:
                self.surya_engine = SuryaOCREngine(self.logger)
            
            results = self.surya_engine.process_pages(pages, year_dir, pdf_paths)
            for page, res in zip(pages, results):
                if res['success']:
                    self.logger.info(f"  Page {page.page_num} - Tier 2 OCR (Surya): Success, {res['word_count']} words")
                else:
                    self.logger.error(f"  Page {page.page_num} - Tier 2 OCR (Surya): Failed: {res.get('error')}")
