from flask import Blueprint, render_template, request, jsonify, send_from_directory, current_app, abort, url_for, session, redirect, flash
import os
import base64
from io import BytesIO
from PIL import Image
from routes import state as conf_state

photos_bp = Blueprint('photos', __name__, url_prefix='/photos')

# Legacy flat dirs (read fallback only); new files go to storage/<conf_key>/*.
LEGACY_PREVIEW, LEGACY_GOOD, LEGACY_ORIGINAL = conf_state.legacy_dirs()
os.makedirs(LEGACY_PREVIEW, exist_ok=True)
os.makedirs(LEGACY_GOOD, exist_ok=True)
os.makedirs(LEGACY_ORIGINAL, exist_ok=True)


def _sel():
    """Return (conf_schema, conf_key) for this browser's selected conference."""
    sel = session.get('selected_conference') or {}
    return sel.get('conf_schema'), sel.get('conf_key')


def _require_sel():
    """Return (conf_schema, conf_key) or a redirect response if none selected."""
    conf_schema, conf_key = _sel()
    if not conf_schema:
        flash("Please select a conference first.", "warning")
        return None, None, redirect(url_for('dashboard.dashboard'))
    return conf_schema, conf_key, None


def _is_app_uploaded_row(rec, conf_key=None):
    """Strict app-upload check shared with dashboard worker: base path + filename pattern."""
    try:
        import re as _re
        path = str((rec or {}).get('del_img_path') or '').strip()
        fname = str((rec or {}).get('del_img_filename') or '').strip()
        if not path or not fname or 'amazonaws.com' not in path.lower():
            return False
        if conf_key:
            try:
                from services import s3_service as _s3
                expected = str(_s3.s3_base_path(conf_key) or '').strip().rstrip('/') + '/'
                if path.strip().rstrip('/').lower() != expected.strip().rstrip('/').lower():
                    return False
            except Exception:
                pass
        try:
            did = int((rec or {}).get('delegate_id') or 0)
        except (TypeError, ValueError):
            did = 0
        if did and not fname.startswith(f"{did}-"):
            return False
        return bool(_re.match(r'^.+-\d{8}-\d{6}\.jpe?g$', fname, _re.IGNORECASE))
    except Exception:
        return False


def _scan_dir_for_entries(d, status, conf_schema, detail, original_dir, conf_key=None):
    """Scan one preview/good dir and register missing ids for this conference."""
    if not d or not os.path.exists(d):
        return
    for fname in os.listdir(d):
        if not fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
            continue
        # filename is {delegate_id}.jpg or {delegate_id}_original.* (skip originals)
        if '_original' in fname:
            continue
        try:
            delegate_id = int(os.path.splitext(fname)[0])
        except ValueError:
            continue
        if conf_state.get_entry(conf_schema, delegate_id) is not None:
            continue
        # Try to find delegate record in this conference's schema DB
        rec = None
        try:
            from db.database import get_connection
            conn = get_connection(database=conf_schema)
            try:
                with conn.cursor() as cur:
                    try:
                        cur.execute("SELECT delegate_id, delegate_no, full_name, email, mobile, del_img_path, del_img_filename, profile_photo_status FROM delegates WHERE delegate_id=%s", (delegate_id,))
                    except Exception:
                        cur.execute("SELECT delegate_id, delegate_no, full_name, email, mobile, del_img_path, del_img_filename FROM delegates WHERE delegate_id=%s", (delegate_id,))
                    row = cur.fetchone()
                    if row:
                        rec = row
            finally:
                conn.close()
        except Exception:
            rec = None
        if not rec:
            rec = {'delegate_id': delegate_id, 'full_name': f'Delegate {delegate_id}', 'delegate_no': delegate_id, 'email': '', 'mobile': '', 'del_img_filename': fname}
        preview_path = os.path.join(d, fname)
        # Find original path if exists
        orig_path = None
        if os.path.exists(original_dir):
            for of in os.listdir(original_dir):
                if of.startswith(f"{delegate_id}_original"):
                    orig_path = os.path.join(original_dir, of)
                    break
        # Build original_url from DB record if possible
        orig_url = None
        try:
            from services.photo_service import build_photo_url
            orig_url = build_photo_url(rec.get('del_img_path'), rec.get('del_img_filename'))
        except Exception:
            orig_url = None
        # If DB row is a photo uploaded by THIS app (strict base-path +
        # "<id>-Name-timestamp.jpg" check), restore as GOOD + UPLOADED.
        # Raw S3 originals (registration uploads) stay with disk status so
        # the next Start Processing crops them instead of faking GOOD.
        restored_status = status
        restored_detail = 'restored from disk' if status == 'GOOD' else detail
        restored_s3_status = 'PENDING'
        restored_s3_key = None
        restored_s3_url = None
        try:
            if _is_app_uploaded_row(rec, conf_key):
                restored_status = 'GOOD'
                restored_detail = 'uploaded to S3'
                restored_s3_status = 'UPLOADED'
                restored_s3_url = orig_url
                if orig_url and '.amazonaws.com/' in orig_url:
                    restored_s3_key = orig_url.split('.amazonaws.com/', 1)[1]
        except Exception:
            pass
        conf_state.set_entry(conf_schema, delegate_id, {
            'record': rec,
            'preview_path': preview_path,
            'original_path': orig_path,
            'original_url': orig_url,
            'status': restored_status,
            'detail': restored_detail,
            'bbox': None,
            's3_status': restored_s3_status,
            's3_key': restored_s3_key,
            's3_url': restored_s3_url,
            's3_error': '',
            'uploaded_at': None,
        })


def _ensure_photos_loaded(conf_schema, conf_key):
    """If this conference has no entries after restart, rebuild from its
    storage/<conf_key>/ files (plus legacy flat files as fallback) and DB.
    Other conferences' entries are never touched.
    """
    if not conf_schema:
        return
    if conf_state.scoped_entries(conf_schema):
        return
    preview_dir, good_dir, original_dir = conf_state.conf_dirs(conf_key)
    # Conference folders first (authoritative for this conference)
    _scan_dir_for_entries(preview_dir, 'AUTO_UPDATED', conf_schema, 'cropped to 354x472', original_dir, conf_key)
    _scan_dir_for_entries(good_dir, 'GOOD', conf_schema, 'restored from disk', original_dir, conf_key)
    # Legacy flat folders as fallback for pre-isolation files
    _scan_dir_for_entries(LEGACY_PREVIEW, 'AUTO_UPDATED', conf_schema, 'cropped to 354x472', LEGACY_ORIGINAL, conf_key)
    _scan_dir_for_entries(LEGACY_GOOD, 'GOOD', conf_schema, 'restored from disk', LEGACY_ORIGINAL, conf_key)
    # Legacy originals: attach to entries missing original_path
    for legacy_orig in (LEGACY_ORIGINAL,):
        if not os.path.exists(legacy_orig):
            continue
        for of in os.listdir(legacy_orig):
            if '_original' not in of:
                continue
            try:
                did = int(of.split('_original')[0])
            except ValueError:
                continue
            entry = conf_state.get_entry(conf_schema, did)
            if entry is not None and not entry.get('original_path'):
                entry['original_path'] = os.path.join(legacy_orig, of)


def _find_file(filename, conf_key):
    """Locate a preview/good file: conference folders first, then legacy flat."""
    if not filename or '/' in filename or '\\' in filename:
        return None
    preview_dir, good_dir, _od = conf_state.conf_dirs(conf_key)
    for d in (preview_dir, good_dir, LEGACY_PREVIEW, LEGACY_GOOD):
        p = os.path.join(d, filename)
        if os.path.exists(p):
            return p
    return None


def _find_original(delegate_id, conf_key):
    """Locate {delegate_id}_original.* : conference folder first, then legacy."""
    _pv, _gd, original_dir = conf_state.conf_dirs(conf_key)
    for d in (original_dir, LEGACY_ORIGINAL):
        if os.path.exists(d):
            for fname in os.listdir(d):
                if fname.startswith(f"{delegate_id}_original"):
                    return os.path.join(d, fname)
    return None


@photos_bp.route('/')
def photos_list():
    """View all photos with name and photo status: good, auto_updated, manual_updated, failed with count.
    Supports ?status=good|auto_updated|manual_updated|failed|all and ?page=&per_page=
    Scoped to THIS browser's selected conference only.
    Step 3 & 4.
    """
    conf_schema, conf_key, redir = _require_sel()
    if redir:
        return redir
    status_filter = request.args.get('status', 'all')  # good, auto_updated, manual_updated, failed, all
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 100))
    # Clamp per_page to valid range
    valid_per_page = [100, 200, 500, 1000]
    if per_page not in valid_per_page:
        per_page = 100
    search = request.args.get('q', '').strip().lower()

    _ensure_photos_loaded(conf_schema, conf_key)
    entries = conf_state.scoped_entries(conf_schema)
    # One-time migration: older manual crops were stored as AUTO_UPDATED
    # with detail 'manual crop 354x472' — flip them to MANUAL_UPDATED.
    for _d in entries:
        if _d.get('status') == 'AUTO_UPDATED' and _d.get('detail') == 'manual crop 354x472':
            _d['status'] = 'MANUAL_UPDATED'
    all_items = entries

    # Filter by status
    if status_filter != 'all':
        # Map legacy: good=GOOD, auto_updated=AUTO_UPDATED, manual_updated=MANUAL_UPDATED, failed=FAILED
        status_map = {
            'good': ['GOOD'],
            'auto_updated': ['AUTO_UPDATED'],
            'manual_updated': ['MANUAL_UPDATED'],
            'auto_updated_legacy': ['OK'],
            'failed': ['FAILED'],
        }
        allowed = status_map.get(status_filter, [status_filter.upper()])
        filtered = [d for d in all_items if d.get('status') in allowed]
    else:
        filtered = all_items

    # Search by name/email/mobile
    if search:
        tmp = []
        for d in filtered:
            rec = d.get('record', {})
            hay = f"{rec.get('full_name','')} {rec.get('email','')} {rec.get('mobile','')} {rec.get('delegate_no','')} {d.get('delegate_id','')}".lower()
            # delegate_id is key, not in record? Need to get from keyed store? We store record contains delegate_id
            if search in hay or search in str(d.get('record',{}).get('delegate_id','')):
                tmp.append(d)
        filtered = tmp

    total = len(filtered)
    # Counts for header (always total counts, not filtered)
    counts = {
        'total': len(all_items),
        'good': sum(1 for d in all_items if d.get('status') == 'GOOD'),
        'auto_updated': sum(1 for d in all_items if d.get('status') == 'AUTO_UPDATED'),
        'manual_updated': sum(1 for d in all_items if d.get('status') == 'MANUAL_UPDATED'),
        'failed': sum(1 for d in all_items if d.get('status') == 'FAILED'),
        # Legacy fallback: count OK as auto_updated if present
        'all': len(all_items),
    }
    # Also include sub counts for failed breakdown
    counts['no_face'] = sum(1 for d in all_items if d.get('detail') == 'NO_FACE')
    counts['multiple'] = sum(1 for d in all_items if d.get('detail') == 'MULTIPLE_FACES')

    # Pagination
    start = (page - 1) * per_page
    end = start + per_page
    page_items = filtered[start:end]
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    return render_template('photos.html', items=page_items, counts=counts, status_filter=status_filter,
                           page=page, per_page=per_page, total=total, total_pages=total_pages, search=search)

@photos_bp.route('/image/<path:filename>')
def serve_image(filename):
    """Serve preview/cropped image from THIS conference's folders (legacy fallback)."""
    _conf_schema, conf_key = _sel()
    found = _find_file(os.path.basename(filename), conf_key)
    if found:
        return send_from_directory(os.path.dirname(found), os.path.basename(found))
    abort(404)

@photos_bp.route('/original_image/<int:delegate_id>')
def serve_original_image(delegate_id):
    """Serve original downloaded image from THIS conference's ORIGINAL_DIR.
    Proxies remote S3 if not cached to avoid CORS taint for Cropper.js.
    Never falls back to preview/crop – original must be original.
    """
    conf_schema, conf_key = _sel()
    found = _find_original(delegate_id, conf_key)
    if found:
        return send_from_directory(os.path.dirname(found), os.path.basename(found))
    # Not cached – proxy download remote original_url if available
    data = conf_state.get_entry(conf_schema, delegate_id) if conf_schema else None
    if data and data.get('original_url'):
        try:
            import requests
            url = data['original_url']
            resp = requests.get(url, timeout=10)
            if resp.ok:
                ext = os.path.splitext(url)[1] or '.jpg'
                if '?' in ext:
                    ext = ext.split('?')[0]
                if ext.lower() not in ['.jpg','.jpeg','.png','.webp']:
                    ext = '.jpg'
                _pv, _gd, original_dir = conf_state.conf_dirs(conf_key)
                save_path = os.path.join(original_dir, f"{delegate_id}_original{ext}")
                with open(save_path, 'wb') as f:
                    f.write(resp.content)
                # Update keyed entry original_path
                data['original_path'] = save_path
                return send_from_directory(original_dir, os.path.basename(save_path))
        except Exception:
            pass
        # If proxy fails, redirect to remote for debug (cropper will load cross-origin – may taint)
        # Still try to serve preview as last resort for visibility
        preview_file = os.path.basename(data.get('preview_path') or '')
        if preview_file:
            found_preview = _find_file(preview_file, conf_key)
            if found_preview:
                return send_from_directory(os.path.dirname(found_preview), os.path.basename(found_preview))
        return jsonify({'redirect': data['original_url']}), 302
    # Legacy fallback: try preview/good if no entry
    if data is None:
        preview_file = None
        for d in [LEGACY_PREVIEW, LEGACY_GOOD]:
            if not os.path.exists(d):
                continue
            for fname in os.listdir(d):
                if fname.startswith(str(delegate_id) + '.'):
                    return send_from_directory(d, fname)
    abort(404)

@photos_bp.route('/original/<int:delegate_id>')
def serve_original(delegate_id):
    """Legacy alias for serve_original_image."""
    return serve_original_image(delegate_id)

@photos_bp.route('/info/<int:delegate_id>')
def photo_info(delegate_id):
    """JSON info for manual crop modal: original and cropped URLs (this conference)."""
    conf_schema, conf_key = _sel()
    if not conf_schema:
        abort(404)
    _ensure_photos_loaded(conf_schema, conf_key)
    data = conf_state.get_entry(conf_schema, delegate_id)
    if not data:
        # Fallback: try to build minimal entry from disk + DB even if preview missing
        found_preview = None
        _pv, _gd, _od = conf_state.conf_dirs(conf_key)
        for d in [_pv, _gd, LEGACY_PREVIEW, LEGACY_GOOD]:
            if os.path.exists(d):
                for fname in os.listdir(d):
                    if fname.startswith(f"{delegate_id}."):
                        cand = os.path.join(d, fname)
                        if os.path.isfile(cand):
                            found_preview = cand
                            break
                if found_preview:
                    break
        if not found_preview and not _find_original(delegate_id, conf_key):
            abort(404)
        # Build minimal record from THIS conference's DB
        rec = None
        try:
            from db.database import get_connection
            conn = get_connection(database=conf_schema)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT delegate_id, delegate_no, full_name, email, mobile, del_img_path, del_img_filename FROM delegates WHERE delegate_id=%s", (delegate_id,))
                    row = cur.fetchone()
                    if row:
                        rec = row
            finally:
                conn.close()
        except Exception:
            rec = None
        if not rec:
            rec = {'delegate_id': delegate_id, 'full_name': f'Delegate {delegate_id}'}
        # Build original_url
        orig_url = None
        try:
            from services.photo_service import build_photo_url
            orig_url = build_photo_url(rec.get('del_img_path'), rec.get('del_img_filename'))
        except Exception:
            orig_url = None
        data = {
            'record': rec,
            'preview_path': found_preview,
            'original_path': None,
            'original_url': orig_url,
            'status': 'AUTO_UPDATED' if found_preview else 'FAILED',
            'detail': 'restored',
        }
        conf_state.set_entry(conf_schema, delegate_id, data)
    rec = data.get('record', {})
    # Build URLs with cache-busting timestamp
    import time
    ts = int(time.time())
    original_url = url_for('photos.serve_original_image', delegate_id=delegate_id) + f"?t={ts}"
    preview_file = os.path.basename(data.get('preview_path') or '')
    cropped_url = url_for('photos.serve_image', filename=preview_file) + f"?t={ts}" if preview_file and _find_file(preview_file, conf_key) else None
    # Check original exists
    has_original = _find_original(delegate_id, conf_key) is not None
    # Always serve the original through the same-origin proxy route (which
    # downloads + caches the remote file on demand). Never hand the browser a
    # raw cross-origin S3 URL: without CORS headers the image fails to load
    # (cropper stays empty) and the canvas gets tainted so Save fails.
    return jsonify({
        'delegate_id': delegate_id,
        'full_name': rec.get('full_name',''),
        'original_url': original_url,
        'has_original_file': has_original,
        'cropped_url': cropped_url,
        'status': data.get('status'),
        'detail': data.get('detail'),
        'preview_file': preview_file,
    })

@photos_bp.route('/manual_crop/<int:delegate_id>', methods=['POST'])
def manual_crop(delegate_id):
    """Receive manually cropped image (354x472) from frontend Cropper.js.
    Expects multipart/form-data with 'cropped' file (blob) or JSON base64 'image'.
    Saves to THIS conference's preview folder and updates keyed entry status
    to MANUAL_UPDATED.
    """
    conf_schema, conf_key = _sel()
    if not conf_schema:
        return jsonify({'error': 'No conference selected'}), 400
    try:
        # Try file upload first
        cropped_file = request.files.get('cropped')
        image_data = None
        if cropped_file:
            image_data = cropped_file.read()
        else:
            # Try base64 JSON
            json_data = request.get_json(silent=True)
            if json_data and 'image' in json_data:
                b64 = json_data['image']
                if ',' in b64:
                    b64 = b64.split(',',1)[1]
                image_data = base64.b64decode(b64)
            elif request.form.get('image'):
                b64 = request.form.get('image')
                if ',' in b64:
                    b64 = b64.split(',',1)[1]
                image_data = base64.b64decode(b64)

        if not image_data:
            return jsonify({'error': 'No image data received'}), 400

        # Validate and convert to 354x472 JPEG
        from config import Config
        img = Image.open(BytesIO(image_data))
        # Handle rotation EXIF if any
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        if img.mode != 'RGB':
            img = img.convert('RGB')
        # Resize to exact 354x472 if not already
        out_w = Config.OUTPUT_WIDTH
        out_h = Config.OUTPUT_HEIGHT
        if img.size != (out_w, out_h):
            # Use LANCZOS for quality
            img = img.resize((out_w, out_h), Image.LANCZOS)

        # Save to THIS conference's preview folder
        preview_dir, _gd, original_dir = conf_state.conf_dirs(conf_key)
        output_path = os.path.join(preview_dir, f"{delegate_id}.jpg")
        img.save(output_path, format='JPEG', quality=95)

        # Also save uploaded original if provided as 'original_upload' for future use
        original_upload = request.files.get('original_upload')
        if original_upload:
            ext = os.path.splitext(original_upload.filename)[1] or '.jpg'
            orig_path = os.path.join(original_dir, f"{delegate_id}_original{ext}")
            original_upload.save(orig_path)

        # Update keyed entry
        data = conf_state.get_entry(conf_schema, delegate_id)
        if data:
            data['preview_path'] = output_path
            data['status'] = 'MANUAL_UPDATED'
            data['detail'] = 'manual crop 354x472'
            # New crop needs re-upload even if previously uploaded
            data['s3_status'] = 'PENDING'
            data['s3_error'] = ''
            # Keep original_path if exists
        else:
            # Create minimal entry if not existed (e.g., after restart)
            conf_state.set_entry(conf_schema, delegate_id, {
                'preview_path': output_path,
                'original_path': None,
                'original_url': None,
                'status': 'MANUAL_UPDATED',
                'detail': 'manual crop 354x472',
                'record': {'delegate_id': delegate_id, 'full_name': f'Delegate {delegate_id}'},
                's3_status': 'PENDING',
                's3_key': None,
                's3_url': None,
                's3_error': '',
                'uploaded_at': None,
            })

        # Recount THIS conference's dashboard counters so Good/Auto/Manual/Failed stay accurate
        conf_state.recount_processing_status(conf_schema)

        return jsonify({'success': True, 'preview_url': url_for('photos.serve_image', filename=f"{delegate_id}.jpg"), 'message': 'Manual crop saved 354x472'})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@photos_bp.route('/upload_original/<int:delegate_id>', methods=['POST'])
def upload_original(delegate_id):
    """Upload new original image for delegate (before crop). Stores in THIS conference's ORIGINAL_DIR."""
    conf_schema, conf_key = _sel()
    if not conf_schema:
        return jsonify({'error': 'No conference selected'}), 400
    try:
        f = request.files.get('file') or request.files.get('original')
        if not f:
            return jsonify({'error': 'No file uploaded'}), 400
        ext = os.path.splitext(f.filename)[1] or '.jpg'
        if ext.lower() not in ['.jpg','.jpeg','.png','.webp']:
            ext = '.jpg'
        _pv, _gd, original_dir = conf_state.conf_dirs(conf_key)
        save_path = os.path.join(original_dir, f"{delegate_id}_original{ext}")
        f.save(save_path)
        # Update keyed entry original_path
        data = conf_state.get_entry(conf_schema, delegate_id)
        if data:
            data['original_path'] = save_path
        return jsonify({'success': True, 'original_url': url_for('photos.serve_original_image', delegate_id=delegate_id)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@photos_bp.route('/data')
def photos_data():
    """JSON for debugging / API (scoped to selected conference)."""
    conf_schema, _ck = _sel()
    entries = conf_state.scoped_entries(conf_schema) if conf_schema else []
    return jsonify({
        'counts': {
            'total': len(entries),
            'good': sum(1 for d in entries if d.get('status') == 'GOOD'),
            'auto_updated': sum(1 for d in entries if d.get('status') == 'AUTO_UPDATED'),
            'manual_updated': sum(1 for d in entries if d.get('status') == 'MANUAL_UPDATED'),
            'failed': sum(1 for d in entries if d.get('status') == 'FAILED'),
        },
        'items': entries[:5]
    })
