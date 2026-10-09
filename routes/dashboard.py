from flask import Blueprint, render_template, current_app, redirect, url_for, request, session, flash, jsonify
import threading
import os
from db.database import get_connection
from services.photo_service import get_all_delegates, build_photo_url, download_image
from services.conference_service import get_active_upcoming_conferences, get_conference_by_id
from services.face_detector import detect_face
from services.crop_service import generate_passport_crop, is_passport_photo, copy_as_good
from config import Config
from routes import state as conf_state

dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/dashboard')

# NOTE: per-conference processing state lives in routes/state.py
# (processing_data keyed by (conf_schema, delegate_id), one job/thread per
# conf_schema). Each browser session selects its own conference, so two
# systems can process different conferences in parallel.


def _new_entry(url, local_path, preview_path, status, detail, confidence, bbox, record):
    return {
        'original_url': url,
        'original_path': local_path,
        'preview_path': preview_path,
        'status': status,
        'detail': detail,
        'confidence': confidence,
        'bbox': bbox,
        'record': record,
        's3_status': 'PENDING',
        's3_key': None,
        's3_url': None,
        's3_error': '',
        'uploaded_at': None,
    }


def _is_app_uploaded(record, conf_key=None):
    """True ONLY if this DB row is a photo uploaded by THIS app (not a raw S3 original).

    App uploads always set:
      del_img_path == s3_base_path(conf_key)  (https://<bucket>.s3.<region>.amazonaws.com/<prefix>/<conf_key>/)
      del_img_filename == "<delegate_id>-<Name>-<Ymd>-<His>.jpg" (build_s3_key pattern)
    A registration-form S3 original (different path/filename) must return False
    so it goes through the normal download -> is_passport -> crop pipeline
    instead of being faked as GOOD+UPLOADED.
    """
    try:
        rec = record or {}
        path = str(rec.get('del_img_path') or '').strip()
        fname = str(rec.get('del_img_filename') or '').strip()
        if not path or not fname:
            return False
        low = path.lower().strip()
        if 'amazonaws.com' not in low:
            return False
        # 1) Path must be this conference's app base path (when conf_key known).
        if conf_key:
            try:
                from services import s3_service as _s3
                expected = str(_s3.s3_base_path(conf_key) or '').strip().rstrip('/') + '/'
                # Compare case-insensitively with trailing slash normalized.
                if path.strip().rstrip('/') .lower() != expected.strip().rstrip('/').lower():
                    return False
            except Exception:
                pass
        else:
            # Without conf_key, at least require bucket+prefix markers.
            bucket = str(getattr(Config, 'S3_BUCKET', '') or '').strip().lower()
            prefix = str(getattr(Config, 'S3_PREFIX', '') or 'delegate_photo').strip().strip('/').lower()
            if bucket and bucket not in low:
                return False
            if prefix and prefix not in low:
                return False
        # 2) Filename must match app pattern: "<id>-<Name>-<8digits>-<6digits>.jpg"
        import re as _re
        try:
            did = int(rec.get('delegate_id') or 0)
        except (TypeError, ValueError):
            did = 0
        # Strict: starts with "<delegate_id>-" and ends with timestamp ".jpg"/".jpeg"
        # (older app uploads preserved the original extension, e.g. .jpeg).
        if did and not fname.startswith(f"{did}-"):
            return False
        if not _re.match(r'^.+-\d{8}-\d{6}\.jpe?g$', fname, _re.IGNORECASE):
            return False
        return True
    except Exception:
        return False


def _is_s3_uploaded(record, conf_key=None):
    """Back-compat wrapper: any S3 URL (loose). Prefer _is_app_uploaded()."""
    try:
        path = str(((record or {}).get('del_img_path')) or '').lower().strip()
        if not path:
            return False
        if 'amazonaws.com' in path:
            return True
        bucket = str(getattr(Config, 'S3_BUCKET', '') or '').strip().lower()
        if bucket and bucket in path and path.startswith('http'):
            return True
        return False
    except Exception:
        return False


def _crop_zoom_detail(local_path, bbox):
    """Human-readable zoom info for AUTO detail, e.g. 'from 1200x1600 face 180px zoom x1.2'."""
    try:
        from PIL import Image as _Img
        from config import Config as _Cfg
        img = _Img.open(local_path)
        W, H = img.size
        _x, _y, _w, _h = bbox
        out_h = _Cfg.OUTPUT_HEIGHT
        scale = (_Cfg.FACE_TARGET_RATIO * out_h) / max(1, _h)
        return f"cropped to 354x472 from {W}x{H} face {_h}px zoom x{scale:.2f}", scale, (W, H)
    except Exception:
        return "cropped to 354x472", 0, (0, 0)


def _previews_identical(local_path, preview_path, threshold=8.0):
    """True if the crop is visually a no-op (same framing, just resized)."""
    try:
        from PIL import Image as _Img
        import numpy as _np
        a = _Img.open(local_path).convert('L').resize((64, 64), _Img.BILINEAR)
        b = _Img.open(preview_path).convert('L').resize((64, 64), _Img.BILINEAR)
        diff = (abs(_np.asarray(a, dtype=float) - _np.asarray(b, dtype=float))).mean()
        return diff < threshold
    except Exception:
        return False


def _looks_like_app_file(record):
    """Loose app-pattern check (any conference): '<id>-Name-Ymd-His.jpg' on S3."""
    try:
        import re as _re
        path = str((record or {}).get('del_img_path') or '')
        fname = str((record or {}).get('del_img_filename') or '').strip()
        if 'amazonaws.com' not in path.lower():
            return False
        try:
            did = int((record or {}).get('delegate_id') or 0)
        except (TypeError, ValueError):
            did = 0
        if did and not fname.startswith(f"{did}-"):
            return False
        return bool(_re.match(r'^.+-\d{8}-\d{6}\.jpe?g$', fname, _re.IGNORECASE))
    except Exception:
        return False


def processing_worker(app, conf_id, conf_schema, conf_key=None, force=False):
    """Background worker for ONE conference.

    Updates only that conference's job status and (conf_schema, delegate_id)
    entries — other conferences' data/threads are never touched.
    Runs inside app_context.
    Uses per-conference schema (e.g., numerotech_conf_roscon26) if available.
    """
    job = conf_state.get_processing_job(conf_schema)
    st = job['status']
    with app.app_context():
        try:
            job['is_processing'] = True
            batch_size = app.config.get('BATCH_SIZE', 50)
            # Fetch delegates from per-conference DB if schema exists, otherwise filter by conference_id in primary DB
            # For per-conference schemas, only approved delegates (del_status_id=2) with photos are processed — matches user query: SELECT * WHERE del_status_id=2 AND del_img_path IS NOT NULL
            if conf_schema:
                delegates = get_all_delegates(database=conf_schema, filters={'del_status_id': 2})
            else:
                delegates = get_all_delegates(filters={'conference_id': conf_id})
            total = len(delegates)
            conf_state.reset_status(st)
            st['total'] = total

            if total == 0:
                app.logger.info(f"No delegates found for conference_id={conf_id}")
                return

            # Clear only THIS conference's entries (other conferences untouched)
            conf_state.clear_conference_data(conf_schema)

            for i in range(0, total, batch_size):
                batch = delegates[i:i+batch_size]
                for record in batch:
                    delegate_id = record['delegate_id']
                    url = build_photo_url(record['del_img_path'], record['del_img_filename'])
                    # Skip ONLY photos uploaded by THIS app (strict base-path +
                    # "<id>-Name-timestamp.jpg" check): never re-crop them on
                    # re-processing, otherwise GOOD flips back to AUTO_UPDATED.
                    # Raw S3 originals (registration uploads) return False here
                    # and go through the normal crop pipeline below. As a safety
                    # net, an app-pattern file from ANOTHER conference also
                    # skips (already cropped, re-cropping would be a no-op that
                    # shows identical ORIGINAL vs CROPPED).
                    # Manual re-crop sets in-memory MANUAL_UPDATED/PENDING, so
                    # respect that and re-process only when forced or re-edited.
                    if not force and (_is_app_uploaded(record, conf_key) or _looks_like_app_file(record)):
                        try:
                            _mem = conf_state.get_entry(conf_schema, delegate_id)
                        except Exception:
                            _mem = None
                        if not (_mem and _mem.get('status') == 'MANUAL_UPDATED'):
                            local_path = download_image(url, delegate_id, conf_key=conf_key)
                            if local_path:
                                try:
                                    preview_path = copy_as_good(local_path, delegate_id, conf_key=conf_key)
                                except Exception:
                                    preview_path = local_path
                            else:
                                preview_path = None
                            _s3_key = None
                            try:
                                if url and '.amazonaws.com/' in url:
                                    _s3_key = url.split('.amazonaws.com/', 1)[1]
                            except Exception:
                                _s3_key = None
                            _entry = _new_entry(
                                url, local_path, preview_path, 'GOOD', 'uploaded to S3',
                                1.0, None, record)
                            _entry['s3_status'] = 'UPLOADED'
                            _entry['s3_key'] = _s3_key
                            _entry['s3_url'] = url
                            conf_state.set_entry(conf_schema, delegate_id, _entry)
                            st['good'] += 1
                            st['processed'] += 1
                            continue
                    local_path = download_image(url, delegate_id, conf_key=conf_key)
                    if not local_path:
                        conf_state.set_entry(conf_schema, delegate_id, _new_entry(
                            url, None, None, 'FAILED', 'IMAGE_DOWNLOAD_ERROR',
                            None, None, record))
                        st['failed'] += 1
                        st['errors'] += 1
                        st['processed'] += 1
                        continue
                    # Step 2: check if already passport-like (354x472 + face like passport)
                    is_good, reason, good_bbox = is_passport_photo(local_path)
                    if is_good:
                        # Already good — skip crop, mark as good and copy to preview/good
                        preview_path = copy_as_good(local_path, delegate_id, conf_key=conf_key)
                        conf_state.set_entry(conf_schema, delegate_id, _new_entry(
                            url, local_path, preview_path, 'GOOD', reason,
                            1.0, good_bbox, record))
                        st['good'] += 1
                        st['processed'] += 1
                        continue
                    # Not already good — auto crop to passport size
                    detection = detect_face(local_path)
                    status_detail = detection['status']
                    confidence = detection.get('confidence')
                    bbox = detection.get('bbox')
                    preview_path = None
                    if status_detail == 'OK':
                        try:
                            preview_path = generate_passport_crop(local_path, bbox, delegate_id, conf_key=conf_key)
                            status = 'AUTO_UPDATED'
                            detail, _scale, _orig = _crop_zoom_detail(local_path, bbox)
                            try:
                                if preview_path and _previews_identical(local_path, preview_path):
                                    detail += " (minimal change - already close)"
                            except Exception:
                                pass
                            st['auto_updated'] += 1
                            st['auto_approved'] += 1
                        except Exception as e:
                            app.logger.exception(f"Crop failed for {delegate_id}: {e}")
                            status = 'FAILED'
                            detail = 'CROP_ERROR'
                            st['failed'] += 1
                            st['errors'] += 1
                    elif status_detail == 'NO_FACE':
                        status = 'FAILED'
                        detail = 'NO_FACE'
                        st['failed'] += 1
                        st['no_face'] += 1
                        st['needs_review'] += 1
                    elif status_detail == 'MULTIPLE_FACES':
                        status = 'FAILED'
                        detail = 'MULTIPLE_FACES'
                        st['failed'] += 1
                        st['multiple_faces'] += 1
                        st['needs_review'] += 1
                    elif status_detail == 'LOW_CONFIDENCE':
                        status = 'FAILED'
                        detail = 'LOW_CONFIDENCE'
                        st['failed'] += 1
                        st['low_confidence'] += 1
                        st['needs_review'] += 1
                    else:
                        status = 'FAILED'
                        detail = status_detail
                        st['failed'] += 1
                        st['errors'] += 1
                    st['processed'] += 1
                    conf_state.set_entry(conf_schema, delegate_id, _new_entry(
                        url, local_path, preview_path, status, detail,
                        confidence, bbox, record))
        except Exception as e:
            app.logger.exception(f"Processing worker failed: {e}")
        finally:
            job['is_processing'] = False

@dashboard_bp.route('/')
def dashboard():
    # Stats for THIS browser's selected conference only
    selected_conf = session.get('selected_conference')
    if selected_conf and selected_conf.get('conf_schema'):
        stats = conf_state.get_processing_job(selected_conf['conf_schema'])['status']
    else:
        stats = dict(conf_state.STATUS_DEFAULTS)
    # Fetch active & upcoming conferences ordered by start date
    try:
        conferences = get_active_upcoming_conferences()
    except Exception as e:
        conferences = []
        flash(f"Error loading conferences: {e}", "danger")
    return render_template('dashboard.html', stats=stats, selected_conf=selected_conf, conferences=conferences)

@dashboard_bp.route('/start')
def start_processing():
    # Ensure a conference has been selected
    if 'selected_conference' not in session:
        flash("Please select a conference first.", "warning")
        return redirect(url_for('dashboard.dashboard'))
    selected = session['selected_conference']
    conf_id = selected.get('conf_id')
    conf_schema = selected.get('conf_schema')
    conf_key = selected.get('conf_key')
    # Fallback: lookup schema if not in session (backwards compat)
    if not conf_schema:
        try:
            conf = get_conference_by_id(conf_id)
            conf_schema = conf.get('conf_schema') if conf else None
        except Exception:
            conf_schema = None
    if not conf_schema:
        flash("Selected conference has no schema configured.", "danger")
        return redirect(url_for('dashboard.dashboard'))
    job = conf_state.get_processing_job(conf_schema)
    thread = job.get('thread')
    if thread and thread.is_alive():
        flash(f"Processing is already running for {selected.get('conf_name','')}. Other conferences can still run in parallel.", "info")
        return redirect(url_for('dashboard.dashboard'))
    # Pre-check delegate count for immediate feedback
    try:
        if conf_schema:
            delegates_preview = get_all_delegates(database=conf_schema, filters={'del_status_id': 2})
        else:
            delegates_preview = get_all_delegates(filters={'conference_id': conf_id})
        total_preview = len(delegates_preview)
    except Exception as e:
        flash(f"Failed to fetch delegates for conference {conf_id}: {e}", "danger")
        return redirect(url_for('dashboard.dashboard'))
    if total_preview == 0:
        # Update only THIS conference's status; guarantee its worker not running
        job['is_processing'] = False
        conf_state.reset_status(job['status'])
        # Clear any stale data for THIS conference only
        conf_state.clear_conference_data(conf_schema)
        flash(f"No delegates with photos found for {selected.get('conf_name','')} (ID {conf_id}). Nothing to process.", "warning")
        return redirect(url_for('dashboard.dashboard'))
    # Capture app object for thread
    app = current_app._get_current_object()
    # ?force=1 re-crops even S3-uploaded photos (default: skip them as GOOD)
    force = str(request.args.get('force', '')).lower() in ('1', 'true', 'yes')
    # Optimistically set total for UI before thread starts
    conf_state.reset_status(job['status'])
    job['status']['total'] = total_preview
    # Start background processing thread with conference context (per-conference thread)
    thread = threading.Thread(target=processing_worker, args=(app, conf_id, conf_schema, conf_key, force), daemon=True)
    job['thread'] = thread
    thread.start()
    flash(f"Processing started for {selected.get('conf_name','')} (ID {conf_id}) — {total_preview} delegates queued.", "success")
    return redirect(url_for('dashboard.dashboard'))

@dashboard_bp.route('/status')
def dashboard_status():
    """JSON endpoint for polling THIS conference's processing progress."""
    selected_conf = session.get('selected_conference')
    if selected_conf and selected_conf.get('conf_schema'):
        job = conf_state.get_processing_job(selected_conf['conf_schema'])
        data = dict(job['status'])
        data['is_processing'] = job['is_processing']
        thread = job.get('thread')
        data['thread_alive'] = bool(thread and thread.is_alive())
    else:
        data = dict(conf_state.STATUS_DEFAULTS)
        data['is_processing'] = False
        data['thread_alive'] = False
    return jsonify(data)

@dashboard_bp.route('/pause')
def pause_processing():
    # Not implemented for simple threading demo
    return redirect(url_for('dashboard.dashboard'))

@dashboard_bp.route('/resume')
def resume_processing():
    # Not implemented for simple threading demo
    return redirect(url_for('dashboard.dashboard'))

# New route to set conference selection (dropdown - single conf_id, lookup conf_key)
@dashboard_bp.route('/set_conference', methods=['POST'])
def set_conference():
    conf_id = request.form.get('conf_id')
    if not conf_id:
        flash("Please select a conference.", "warning")
        return redirect(url_for('dashboard.dashboard'))
    # Lookup conference details (conf_key, name, start date)
    conf = get_conference_by_id(conf_id)
    if not conf:
        flash(f"Conference not found for id={conf_id}", "danger")
        return redirect(url_for('dashboard.dashboard'))
    # Store in session for later processing steps
    session['selected_conference'] = {
        'conf_id': str(conf['conf_id']),
        'conf_key': conf['conf_key'],
        'conf_name': conf['conf_name'],
        'conf_start_dt': str(conf['conf_start_dt']) if conf.get('conf_start_dt') else str(conf.get('conf_start_time')) if conf.get('conf_start_time') else '',
        'conf_schema': conf.get('conf_schema')
    }
    flash(f"Selected conference: {conf['conf_name']}", "success")
    return redirect(url_for('dashboard.dashboard'))
