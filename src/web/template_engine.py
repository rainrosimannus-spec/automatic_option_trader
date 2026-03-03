"""
Jinja2 templates instance — shared across all routes.
Separated from app.py to avoid circular imports.
"""
from pathlib import Path
from fastapi.templating import Jinja2Templates

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
