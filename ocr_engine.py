import logging
from pathlib import Path
from typing import Dict, Optional

from sources.base import PageMetadata, NewspaperSource

try:
    import fitz  # PyMuPDF
    from PIL import Image
    SURYA_AVAILABLE = True
except ImportError:
    SURYA_AVAILABLE = False


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

    def process_page(self, page: PageMetadata, output_dir: Path, pdf_path: Optional[Path] = None) -> Dict:
        """Process a page using Surya AI models."""
        if not pdf_path or not pdf_path.exists():
            return {'success': False, 'error': f'PDF not found: {pdf_path}'}

        try:
            self._load_models()
            from surya.common.surya.schema import TaskNames
            from datetime import datetime
            
            # Use zoom for better quality
            doc = fitz.open(str(pdf_path))
            fitz_page = doc.load_page(0)
            zoom = 1.5
            mat = fitz.Matrix(zoom, zoom)
            pix = fitz_page.get_pixmap(matrix=mat)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            doc.close()

            # 1. Layout & OCR
            self.logger.debug(f"Running Surya on {pdf_path.name}")
            layout_predictions = self.layout_predictor([img])
            ocr_predictions = self.rec_predictor(
                [img], 
                task_names=[TaskNames.ocr_with_boxes], 
                det_predictor=self.det_predictor
            )
            
            layout_result = layout_predictions[0]
            ocr_result = ocr_predictions[0]

            full_text = "\n".join([line.text for line in ocr_result.text_lines])
            
            # Save
            filename = f"{page.issue_date}_ed-{page.edition}_page{page.page_num:02d}_surya.txt"
            output_path = output_dir / filename
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            header = (
                f"# OCR Text — {page.lccn} — {page.issue_date}\n"
                f"# Page: {page.page_num}\n"
                f"# OCR Method: surya-ai\n"
                f"# ---\n\n"
            )
            
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(header + full_text)

            return {
                'success': True,
                'method': 'surya',
                'text_file': filename,
                'text_path': str(output_path),
                'word_count': len(full_text.split())
            }
        except Exception as e:
            self.logger.error(f"Surya OCR failed: {e}")
            return {'success': False, 'error': str(e)}

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
            if not self.surya_engine:
                self.surya_engine = SuryaOCREngine(self.logger)
            
            res = self.surya_engine.process_page(page, year_dir, pdf_path)
            if res['success']:
                self.logger.info(f"  Tier 2 OCR (Surya): Success, {res['word_count']} words")
            else:
                self.logger.error(f"  Tier 2 OCR (Surya): Failed: {res.get('error')}")
