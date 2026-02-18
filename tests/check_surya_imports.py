import sys

print(f"Python version: {sys.version}")

try:
    import fitz
    print("PyMuPDF (fitz) imported successfully")
except ImportError as e:
    print(f"PyMuPDF NOT found: {e}")

try:
    from PIL import Image
    print("Pillow (PIL) imported successfully")
except ImportError as e:
    print(f"Pillow NOT found: {e}")

try:
    from surya.ocr import run_ocr
    print("surya.ocr imported successfully")
except Exception as e:
    print(f"surya.ocr NOT found/failed: {e}")
    import traceback
    traceback.print_exc()

try:
    from surya.layout import run_layout
    print("surya.layout imported successfully")
except Exception as e:
    print(f"surya.layout NOT found/failed: {e}")

try:
    from surya.model.detection.model import load_model as load_det_model
    print("surya detection model imported successfully")
except Exception as e:
    print(f"surya detection model NOT found/failed: {e}")
