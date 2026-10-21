from flask import Blueprint, render_template, request, jsonify, send_from_directory, current_app, abort, url_for, session, redirect, flash
import os
import base64
from io import BytesIO
from PIL import Image
from routes import state as conf_state
from services.conference_service import get_registration_stats

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
            'from_disk': True,
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
    _scan_dir_for_entries(preview_dir, 'AUTO_UPDATED', conf_schema, 'cropped to 354x455', original_dir, conf_key)
    _scan_dir_for_entries(good_dir, 'GOOD', conf_schema, 'restored from disk', original_dir, conf_key)
    # Legacy flat folders as fallback for pre-isolation files
    _scan_dir_for_entries(LEGACY_PREVIEW, 'AUTO_UPDATED', conf_schema, 'cropped to 354x455', LEGACY_ORIGINAL, conf_key)
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


def _find_preview_for(delegate_id, conf_key):
    """Locate this delegate's cropped preview ({delegate_id}.jpg preferred)."""
    found = _find_file(f"{delegate_id}.jpg", conf_key)
    if found:
        return found
    # Fallback: any {delegate_id}.* image in preview/good dirs
    try:
        preview_dir, good_dir, _od = conf_state.conf_dirs(conf_key)
        for d in (preview_dir, good_dir, LEGACY_PREVIEW, LEGACY_GOOD):
            if not os.path.exists(d):
                continue
            for fname in os.listdir(d):
                if fname.startswith(f"{delegate_id}.") and fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                    return os.path.join(d, fname)
    except Exception:
        pass
    return None


DB_VIEWS = ('registered', 'photos', 'pending', 'done', 'rejected', 'no_photo')

DB_VIEW_LABELS = {
    'registered': 'Registered Delegates',
    'photos': 'Total Photos',
    'pending': 'Verification Pending',
    'done': 'Verification Done',
    'rejected': 'Rejected Photos',
    'no_photo': 'No Photos',
}


def _db_view_entries(conf_schema, conf_key, view):
    """Build photo-list items straight from the delegates table.

    Used by dashboard overview-card links (?view=pending etc.) so counts
    always match the DB, even before Start Processing runs. Display status
    is derived from profile_photo_status: 2->GOOD, 0->PENDING, 1->REJECTED.
    """
    if view == 'photos':
        where = "del_status_id = 2 AND del_img_filename IS NOT NULL AND del_img_filename != ''"
    elif view == 'pending':
        where = "del_status_id = 2 AND profile_photo_status = 0 AND del_img_filename IS NOT NULL AND del_img_filename != ''"
    elif view == 'done':
        where = "del_status_id = 2 AND profile_photo_status = 2"
    elif view == 'rejected':
        where = "del_status_id = 2 AND profile_photo_status = 1"
    elif view == 'no_photo':
        where = "del_status_id = 2 AND (del_img_filename IS NULL OR del_img_filename = '')"
    else:  # registered
        where = "del_status_id = 2"
    try:
        from db.database import get_connection
        conn = get_connection(database=conf_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(f"""SELECT delegate_id, delegate_no, full_name, email, mobile,
                                       del_img_path, del_img_filename, profile_photo_status
                                FROM delegates WHERE {where} ORDER BY delegate_id""")
                rows = cur.fetchall() or []
        finally:
            conn.close()
    except Exception:
        return []
    from services.photo_service import build_photo_url
    items = []
    for rec in rows:
        did = rec.get('delegate_id')
        try:
            flag = int(rec.get('profile_photo_status')) if rec.get('profile_photo_status') is not None else -1
        except (TypeError, ValueError):
            flag = -1
        if flag == 2:
            status, detail = 'GOOD', 'verified (status 2)'
        elif flag == 1:
            status, detail = 'REJECTED', 'rejected (status 1)'
        elif not (rec.get('del_img_filename') or '').strip():
            status, detail = 'NO_PHOTO', 'no photo uploaded'
        else:
            status, detail = 'PENDING', 'pending verification (status 0)'
        try:
            orig_url = build_photo_url(rec.get('del_img_path'), rec.get('del_img_filename'))
        except Exception:
            orig_url = None
        original_file = _find_original(did, conf_key)
        # DB views must not show crops left on disk by an earlier run: before
        # Start Processing (or a manual Crop) there is no session result for
        # this delegate. Entries rebuilt from disk are flagged 'from_disk'.
        try:
            mem = conf_state.get_entry(conf_schema, did) if did else None
        except Exception:
            mem = None
        mem_status = (mem or {}).get('status') if mem else None
        fresh = bool(mem) and not (mem or {}).get('from_disk')

        preview_file = None
        if flag == 2:
            # Verified/uploaded in the DB: show the local passport copy when
            # available (template falls back to the DB original URL).
            preview_file = _find_preview_for(did, conf_key)
        elif fresh:
            mem_pv = (mem or {}).get('preview_path')
            if mem_pv and os.path.exists(mem_pv):
                preview_file = mem_pv
            elif mem_status in ('AUTO_UPDATED', 'MANUAL_UPDATED'):
                preview_file = _find_preview_for(did, conf_key)

        if fresh and mem_status in ('AUTO_UPDATED', 'MANUAL_UPDATED') and preview_file:
            # A fresh / re-crop is upload-eligible even when the DB row is
            # already verified (profile_photo_status=2). Without this,
            # re-cropping a Done delegate in ?view=registered fell back to
            # GOOD and the S3 checkbox / upload buttons never appeared.
            status = mem_status
            detail = (mem or {}).get('detail') or 'cropped to 354x455'
        s3_status, s3_key, s3_url = 'PENDING', None, None
        try:
            if _is_app_uploaded_row(rec, conf_key):
                s3_status = 'UPLOADED'
                s3_url = orig_url
                if orig_url and '.amazonaws.com/' in orig_url:
                    s3_key = orig_url.split('.amazonaws.com/', 1)[1]
        except Exception:
            pass
        items.append({
            'record': rec,
            'preview_path': preview_file,
            'original_path': original_file,
            'original_url': orig_url,
            'status': status,
            'detail': detail,
            'bbox': None,
            's3_status': s3_status,
            's3_key': s3_key,
            's3_url': s3_url,
            's3_error': '',
            'uploaded_at': None,
        })
    return items


def _find_original(delegate_id, conf_key):
    """Locate {delegate_id}_original.* : conference folder first, then legacy."""
    _pv, _gd, original_dir = conf_state.conf_dirs(conf_key)
    for d in (original_dir, LEGACY_ORIGINAL):
        if os.path.exists(d):
            for fname in os.listdir(d):
                if fname.startswith(f"{delegate_id}_original"):
                    return os.path.join(d, fname)
    return None


def _db_original_url(conf_schema, delegate_id):
    """Build the remote original URL straight from the delegates table.

    Used as a fallback when there is no in-memory entry / local cache
    (e.g. ?view=registered opened before Start Processing ever ran).
    Returns None when the row has no photo.
    """
    if not conf_schema or not delegate_id:
        return None
    try:
        from db.database import get_connection
        from services.photo_service import build_photo_url
        conn = get_connection(database=conf_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT del_img_path, del_img_filename FROM delegates WHERE delegate_id=%s",
                    (delegate_id,))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return build_photo_url(row.get('del_img_path'), row.get('del_img_filename'))
    except Exception:
        return None


def _download_and_cache_original(url, delegate_id, conf_key, entry=None):
    """Download remote original to this conference's ORIGINAL_DIR and serve it.

    Returns a Flask response (send_from_directory) on success, else None.
    """
    if not url:
        return None
    try:
        import requests
        resp = requests.get(url, timeout=15)
        if not resp.ok or not resp.content:
            return None
        ext = os.path.splitext(url.split('?')[0])[1] or '.jpg'
        if ext.lower() not in ['.jpg', '.jpeg', '.png', '.webp']:
            ext = '.jpg'
        _pv, _gd, original_dir = conf_state.conf_dirs(conf_key)
        os.makedirs(original_dir, exist_ok=True)
        save_path = os.path.join(original_dir, f"{delegate_id}_original{ext}")
        with open(save_path, 'wb') as f:
            f.write(resp.content)
        if entry is not None:
            entry['original_path'] = save_path
            if not entry.get('original_url'):
                entry['original_url'] = url
        return send_from_directory(original_dir, os.path.basename(save_path))
    except Exception:
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
    # Live registration / verification counts (same row as dashboard)
    try:
        db_stats = get_registration_stats(conf_schema)
    except Exception:
        db_stats = {'registered': 0, 'photos': 0, 'done': 0, 'pending': 0, 'rejected': 0, 'no_photo': 0}
    status_filter = request.args.get('status', 'all')  # good, auto_updated, manual_updated, failed, all
    db_view = request.args.get('view', '').strip().lower()  # overview: registered|photos|pending|done|rejected|no_photo
    if db_view not in DB_VIEWS:
        db_view = ''
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 100))
    # Clamp per_page to valid range
    valid_per_page = [100, 200, 500, 1000]
    if per_page not in valid_per_page:
        per_page = 100
    search = request.args.get('q', '').strip().lower()

    if db_view:
        # DB-backed view for dashboard overview cards: always matches DB counts.
        # Still warm the in-memory store from disk so the S3 upload worker
        # (which looks up entries via conf_state.get_entry) can find freshly
        # cropped previews even when this DB view is opened first.
        try:
            _ensure_photos_loaded(conf_schema, conf_key)
        except Exception:
            pass
        all_items = _db_view_entries(conf_schema, conf_key, db_view)
        filtered = all_items
    else:
        _ensure_photos_loaded(conf_schema, conf_key)
        entries = conf_state.scoped_entries(conf_schema)
        # One-time migration: older manual crops were stored as AUTO_UPDATED
        # with detail 'manual crop 354x455' — flip them to MANUAL_UPDATED.
        for _d in entries:
            if _d.get('status') == 'AUTO_UPDATED' and _d.get('detail') == 'manual crop 354x455':
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

    # Processing-session counts (good / auto / manual / failed). Derived from
    # this conference's in-memory store. DB views count only results produced
    # in THIS session (not entries rebuilt from disk after a restart), so the
    # row reads 0 before Start Processing — matching what the DB view shows.
    proc_entries = conf_state.scoped_entries(conf_schema)
    if db_view:
        proc_entries = [d for d in proc_entries if not d.get('from_disk')]
    proc_counts = {
        'total': len(proc_entries),
        'good': sum(1 for d in proc_entries if d.get('status') == 'GOOD'),
        'auto_updated': sum(1 for d in proc_entries if d.get('status') in ('AUTO_UPDATED', 'OK')),
        'manual_updated': sum(1 for d in proc_entries if d.get('status') == 'MANUAL_UPDATED'),
        'failed': sum(1 for d in proc_entries if d.get('status') == 'FAILED'),
    }

    total = len(filtered)
    # Counts for header (always total counts, not filtered)
    if db_view:
        # DB view uses its own statuses: GOOD=done, PENDING, REJECTED
        counts = {
            'total': len(all_items),
            'good': sum(1 for d in all_items if d.get('status') == 'GOOD'),
            'auto_updated': sum(1 for d in all_items if d.get('status') == 'PENDING'),
            'manual_updated': 0,
            'failed': sum(1 for d in all_items if d.get('status') == 'REJECTED'),
            'all': len(all_items),
        }
    else:
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

    return render_template('photos.html', items=page_items, counts=counts, proc_counts=proc_counts,
                           status_filter=status_filter, page=page, per_page=per_page, total=total,
                           total_pages=total_pages, search=search, db_view=db_view,
                           db_view_label=DB_VIEW_LABELS.get(db_view, ''), db_stats=db_stats)

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
    # Not cached – proxy download remote original. Resolve the URL from the
    # in-memory entry first, then straight from the DB (DB views such as
    # ?view=registered have no entry/cache before Start Processing runs).
    data = conf_state.get_entry(conf_schema, delegate_id) if conf_schema else None
    url = (data or {}).get('original_url') if data else None
    if not url:
        url = _db_original_url(conf_schema, delegate_id)
        if data is not None and url and not data.get('original_url'):
            data['original_url'] = url
    if url:
        served = _download_and_cache_original(url, delegate_id, conf_key, entry=data)
        if served is not None:
            return served
        # If proxy download fails, serve the cropped preview as a last
        # resort for visibility (better than a broken popup).
        if data:
            preview_file = os.path.basename(data.get('preview_path') or '')
            if preview_file:
                found_preview = _find_file(preview_file, conf_key)
                if found_preview:
                    return send_from_directory(os.path.dirname(found_preview), os.path.basename(found_preview))
        return jsonify({'redirect': url}), 302
    # Legacy fallback: try preview/good if no URL at all
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
        # Build minimal record from THIS conference's DB first: DB views
        # (?view=registered etc.) list rows that were never processed, so
        # there may be no local preview/original file yet. As long as the
        # DB row has a photo, the crop popup must still open — the
        # /original_image proxy downloads + caches it on demand.
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
        if not found_preview and not _find_original(delegate_id, conf_key) and not orig_url:
            abort(404)
        data = {
            'record': rec,
            'preview_path': found_preview,
            'original_path': None,
            'original_url': orig_url,
            'status': 'AUTO_UPDATED' if found_preview else 'FAILED',
            'detail': 'restored',
            'from_disk': True,
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
        # True when the delegates row actually has a remote photo (lets the
        # popup show "use Upload New" instead of a doomed proxy load).
        'has_remote_photo': bool(data.get('original_url')),
        'cropped_url': cropped_url,
        'status': data.get('status'),
        'detail': data.get('detail'),
        'preview_file': preview_file,
    })

@photos_bp.route('/manual_crop/<int:delegate_id>', methods=['POST'])
def manual_crop(delegate_id):
    """Receive manually cropped image (354x455) from frontend Cropper.js.
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

        # Validate and convert to 354x455 JPEG
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
        # Resize to exact 354x455 if not already
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
            data['detail'] = 'manual crop 354x455'
            # New crop needs re-upload even if previously uploaded
            data['s3_status'] = 'PENDING'
            data['s3_error'] = ''
            # This is a crop made in the current session, even if the entry
            # was originally rebuilt from disk.
            data['from_disk'] = False
            # Keep original_path if exists
        else:
            # Create minimal entry if not existed (e.g., after restart)
            conf_state.set_entry(conf_schema, delegate_id, {
                'preview_path': output_path,
                'original_path': None,
                'original_url': None,
                'status': 'MANUAL_UPDATED',
                'detail': 'manual crop 354x455',
                'record': {'delegate_id': delegate_id, 'full_name': f'Delegate {delegate_id}'},
                's3_status': 'PENDING',
                's3_key': None,
                's3_url': None,
                's3_error': '',
                'uploaded_at': None,
                'from_disk': False,
            })

        # Recount THIS conference's dashboard counters so Good/Auto/Manual/Failed stay accurate
        conf_state.recount_processing_status(conf_schema)

        return jsonify({'success': True, 'preview_url': url_for('photos.serve_image', filename=f"{delegate_id}.jpg"), 'message': 'Manual crop saved 354x455'})
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

@photos_bp.route('/update_status/<int:delegate_id>', methods=['POST'])
def update_photo_status(delegate_id):
    """Update delegate's photo verification status (profile_photo_status: 0=pending, 1=rejected, 2=done).
    Scoped to THIS browser's selected conference schema. Also syncs in-memory entry.
    """
    conf_schema, _ck = _sel()
    if not conf_schema:
        return jsonify({'error': 'No conference selected'}), 400
    payload = request.get_json(silent=True) or {}
    # Accept form-encoded too (fallback)
    raw = payload.get('status', request.form.get('status', None))
    try:
        new_status = int(raw)
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid status. Use 0 (pending), 1 (rejected) or 2 (done).'}), 400
    if new_status not in (0, 1, 2):
        return jsonify({'error': 'Invalid status. Use 0 (pending), 1 (rejected) or 2 (done).'}), 400
    try:
        from db.database import get_connection
        conn = get_connection(database=conf_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE delegates SET profile_photo_status=%s WHERE delegate_id=%s",
                    (new_status, delegate_id))
                if cur.rowcount == 0:
                    conn.rollback()
                    return jsonify({'error': f'Delegate {delegate_id} not found'}), 404
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    # Sync in-memory entry so processed view reflects change without restart
    try:
        entry = conf_state.get_entry(conf_schema, delegate_id)
        if entry is not None:
            rec = entry.get('record') or {}
            rec['profile_photo_status'] = new_status
            # Keep display status/detail in sync for DB-backed views
            if new_status == 2:
                entry['status'] = 'GOOD'
                entry['detail'] = 'verified (status 2)'
            elif new_status == 1:
                entry['status'] = 'REJECTED'
                entry['detail'] = 'rejected (status 1)'
    except Exception:
        pass
    labels = {0: 'verification pending', 1: 'rejected', 2: 'verification done'}
    return jsonify({'success': True, 'delegate_id': delegate_id,
                    'profile_photo_status': new_status,
                    'label': labels.get(new_status, '')})

@photos_bp.route('/remove/<int:delegate_id>', methods=['POST'])
def remove_photo(delegate_id):
    """Remove photo for one delegate: backup row to del_profile_logs, then
    clear del_img_path/del_img_filename in DB (sets NO_PHOTO), delete local
    preview/good/original files, drop in-memory entry. Scoped to selected conf_schema."""
    conf_schema, conf_key = _sel()
    if not conf_schema:
        return jsonify({'success': False, 'error': 'No conference selected'}), 400
    sel = session.get('selected_conference') or {}
    conf_id = sel.get('conf_id')
    # 1) DB: backup + clear in one txn
    try:
        from db.database import get_connection
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            _now = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
        except Exception:
            from datetime import timedelta, timezone
            _ist = timezone(timedelta(hours=5, minutes=30))
            _now = datetime.now(_ist).replace(tzinfo=None)
        conn = get_connection(database=conf_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM delegates WHERE delegate_id=%s FOR UPDATE", (delegate_id,))
                old_row = cur.fetchone()
                if not old_row:
                    conn.rollback()
                    return jsonify({'success': False, 'error': f'Delegate {delegate_id} not found'}), 404
                # Backup full old row into del_profile_logs (if table exists)
                try:
                    cur.execute("SHOW COLUMNS FROM `del_profile_logs`")
                    log_cols = [r['Field'] for r in cur.fetchall()]
                    insert_cols = [c for c in old_row.keys() if c in log_cols and c != 'log_id']
                    vals = [old_row[c] for c in insert_cols]

                    def _set(name, value):
                        if name not in log_cols:
                            return
                        if name in insert_cols:
                            vals[insert_cols.index(name)] = value
                        else:
                            insert_cols.append(name)
                            vals.append(value)
                    if conf_id is not None:
                        try:
                            _set('conf_id', int(conf_id))
                        except (TypeError, ValueError):
                            _set('conf_id', conf_id)
                    _set('action', 'Manual remove in Photo crop app')
                    _set('created_at', _now)
                    _set('updated_at', _now)
                    if insert_cols:
                        ph = ', '.join(['%s'] * len(insert_cols))
                        cl = ', '.join([f"`{c}`" for c in insert_cols])
                        cur.execute(f"INSERT INTO del_profile_logs ({cl}) VALUES ({ph})", vals)
                except Exception:
                    pass  # don't block remove if logs table missing
                # Clear photo columns (only existing cols)
                cur.execute("SHOW COLUMNS FROM `delegates`")
                del_cols = [r['Field'] for r in cur.fetchall()]
                sets, params = [], []
                if 'del_img_path' in del_cols:
                    sets.append("del_img_path=%s")
                    params.append('')
                if 'del_img_filename' in del_cols:
                    sets.append("del_img_filename=%s")
                    params.append('')
                if 'profile_photo_status' in del_cols:
                    sets.append("profile_photo_status=%s")
                    params.append(0)
                if 'updated_at' in del_cols:
                    sets.append("updated_at=NOW()")
                if not sets:
                    conn.rollback()
                    return jsonify({'success': False, 'error': 'No photo columns to clear'}), 500
                params.append(delegate_id)
                cur.execute(f"UPDATE delegates SET {', '.join(sets)} WHERE delegate_id=%s", params)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)[:300]}), 500
    # 2) Delete local files (preview/good/original, conf dirs + legacy)
    removed = []
    try:
        targets = []
        pv = _find_preview_for(delegate_id, conf_key)
        if pv:
            targets.append(pv)
        og = _find_original(delegate_id, conf_key)
        if og:
            targets.append(og)
        # good-copy may differ from preview path
        try:
            _pv_dir, _gd_dir, _od_dir = conf_state.conf_dirs(conf_key)
            for d, pat in ((_pv_dir, f"{delegate_id}."), (_gd_dir, f"{delegate_id}.")):
                if d and os.path.exists(d):
                    for fname in os.listdir(d):
                        if fname.startswith(pat):
                            p = os.path.join(d, fname)
                            if p not in targets:
                                targets.append(p)
        except Exception:
            pass
        for p in targets:
            try:
                if p and os.path.isfile(p):
                    os.remove(p)
                    removed.append(os.path.basename(p))
            except Exception:
                pass
    except Exception:
        pass
    # 3) Drop in-memory entry so processed view stops showing stale crop
    try:
        conf_state.processing_data.pop(conf_state.data_key(conf_schema, delegate_id), None)
    except Exception:
        pass
    try:
        conf_state.recount_processing_status(conf_schema)
    except Exception:
        pass
    return jsonify({'success': True, 'delegate_id': delegate_id, 'removed_files': removed})


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
