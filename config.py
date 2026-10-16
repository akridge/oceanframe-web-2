from pathlib import Path

BASE_DIR    = Path(__file__).parent
UPLOAD_DIR  = BASE_DIR / "uploads"
THUMBNAIL_DIR = UPLOAD_DIR / "thumbs"
STATIC_DIR  = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"

MAX_UPLOAD_BYTES = 8 * 1024 ** 3   # 8 GB ceiling
THUMB_WIDTH      = 240              # px wide for stored thumbnails
SESSION_TTL      = 3600             # seconds before session expires