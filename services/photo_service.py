import os
import requests
from config import Config
from db.database import get_connection
from urllib.parse import urljoin

# Directories for images
PREVIEW_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'storage', 'preview'))
ORIGINAL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'storage', 'original'))
os.makedirs(PREVIEW_DIR, exist_ok=True)
os.makedirs(ORIGINAL_DIR, exist_ok=True)

def build_photo_url(del_img_path, del_img_filename):
    """Construct full URL for the original photo.
    Handles both absolute S3 URLs and relative paths.
    """
    if not del_img_path or not del_img_filename:
        return None
    del_img_path = del_img_path.strip()
    del_img_filename = del_img_filename.strip()
    # If del_img_path is already a full URL (e.g., https://...s3...), just join with filename
    if del_img_path.startswith('http://') or del_img_path.startswith('https://'):
        # Ensure single slash between path and filename
        return del_img_path.rstrip('/') + '/' + del_img_filename.lstrip('/')
    # Otherwise use PHOTO_BASE_URL as base
    base = Config.PHOTO_BASE_URL.rstrip('/') + '/'
    path = del_img_path.lstrip('/')
    filename = del_img_filename.lstrip('/')
    full_path = os.path.join(path, filename)
    return urljoin(base, full_path)

def download_image(url, delegate_id, conf_key=None):
    """Download image from url and save to original directory.
    Keeps preview separate so manual crop can choose original vs cropped.
    When conf_key is given the file goes to storage/<conf_key>/original/
    (per-conference isolation); otherwise legacy flat storage/original/.
    Returns local file path or None on failure.
    """
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        # Determine file extension from URL or response header
        ext = os.path.splitext(url)[1] or '.jpg'
        # normalize ext
        if '?' in ext:
            ext = ext.split('?')[0]
        if ext.lower() not in ['.jpg','.jpeg','.png','.webp']:
            ext = '.jpg'
        # Per-conference folder when conf_key given, else legacy flat dir
        if conf_key:
            from routes.state import conf_dirs
            _pv, _gd, target_dir = conf_dirs(conf_key)
        else:
            target_dir = ORIGINAL_DIR
        # Save to ORIGINAL_DIR as {delegate_id}_original{ext} to preserve original
        local_path = os.path.join(target_dir, f"{delegate_id}_original{ext}")
        with open(local_path, 'wb') as f:
            f.write(resp.content)
        # Also keep a copy in preview dir for legacy fallback (if needed pre-crop)
        # but do not overwrite preview crop; just ensure original exists
        return local_path
    except Exception as e:
        # Log error in real app
        return None

def get_all_delegates(filters=None, database: str = None):
    """Fetch delegate records from MySQL.
    `filters` is a dict that can contain additional WHERE clauses.
    `database` overrides the default DB_NAME (used for per-conference schemas like numerotech_conf_roscon26).
    Returns a list of dicts with full delegate info for display (name,email,mobile).
    Step 1: get all photos from delegates table.
    """
    query = """
        SELECT delegate_id, user_id, delegate_no, full_name, email, mobile,
               del_img_path, del_img_filename,
               profile_photo_status, is_photo_upload_at, del_status_id, conference_id
        FROM delegates
        WHERE del_img_path IS NOT NULL AND del_img_path != ''
          AND del_img_filename IS NOT NULL AND del_img_filename != ''
    """
    params = []
    if filters:
        for key, value in filters.items():
            query += f" AND {key} = %s"
            params.append(value)
    query += " ORDER BY delegate_id"
    conn = get_connection(database=database) if database else get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return rows
    finally:
        conn.close()
