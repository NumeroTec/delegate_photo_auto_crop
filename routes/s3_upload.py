"""Bulk S3 upload for AUTO_UPDATED (auto cropped) + MANUAL_UPDATED (hand cropped) photos.

Per delegate:
  1. Upload preview JPEG to S3 (outside DB txn).
  2. In ONE DB txn on <conf_schema>: INSERT full old delegates row into
     del_profile_logs (old del_img_path/filename preserved), then UPDATE
     delegates to new S3 path/filename. Commit together.

Upload workers and progress are tracked PER CONFERENCE (routes/state.py), so
two systems can upload different conferences at the same time. Delegate IDs
are always resolved within the requesting session's conf_schema — a bulk
upload can never touch another conference's delegates, even if delegate_ids
collide across schemas.
"""
import csv
import os
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request, session, current_app

from config import Config
from db.database import get_connection
from routes import state as conf_state
from routes.state import processing_data
from services import s3_service

upload_bp = Blueprint('upload', __name__, url_prefix='/upload')

BACKUP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'storage', 'backup'))
os.makedirs(BACKUP_DIR, exist_ok=True)

# Action stamped on del_profile_logs rows created by the Photo Crop app upload.
LOG_ACTION_BEFORE_CROP = "Before Cropping in Photo crop app"


def _ist_now_naive():
    """Current IST (Asia/Kolkata) time as naive datetime for MySQL DATETIME cols."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    except Exception:
        # Fallback: UTC + 5:30 when zoneinfo data is unavailable
        from datetime import timedelta, timezone
        ist = timezone(timedelta(hours=5, minutes=30))
        return datetime.now(ist).replace(tzinfo=None)


def _ensure_trailing_slash(path):
    """Ensure del_img_path values saved to DB end with a single '/'."""
    if not path:
        return path
    path = str(path).strip()
    if not path:
        return path
    return path.rstrip('/') + '/'


def _match_search(entry, search):
    if not search:
        return True
    rec = entry.get('record', {})
    hay = f"{rec.get('full_name','')} {rec.get('email','')} {rec.get('mobile','')} {rec.get('delegate_no','')} {rec.get('delegate_id','')}".lower()
    return search in hay


def _collect_ids(conf_schema, status_filter='auto_updated', search=''):
    """Collect delegate_ids from THIS conference's entries matching filter.
    AUTO_UPDATED (auto crop) and MANUAL_UPDATED (manual crop) are upload-eligible;
    others are skipped later.
    """
    search = (search or '').strip().lower()
    status_map = {
        'good': ['GOOD'],
        'auto_updated': ['AUTO_UPDATED', 'OK'],
        'manual_updated': ['MANUAL_UPDATED'],
        'failed': ['FAILED'],
        'all': None,
    }
    allowed = status_map.get(status_filter, ['AUTO_UPDATED', 'MANUAL_UPDATED', 'OK'])
    ids = []
    for entry in conf_state.scoped_entries(conf_schema):
        if allowed is not None and entry.get('status') not in allowed:
            continue
        if not _match_search(entry, search):
            continue
        rec = entry.get('record', {}) or {}
        did = rec.get('delegate_id')
        if did:
            try:
                ids.append(int(did))
            except (TypeError, ValueError):
                continue
    return ids


def _table_columns(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM `{table}`")
        return [r['Field'] for r in cur.fetchall()]


def _backup_and_update(conf_schema, delegate_id, new_path, new_filename, conf_id=None):
    """INSERT old delegates row into del_profile_logs, then UPDATE delegates.

    Returns old row dict. Both statements commit together; rollback on error.
    Column lists are resolved at runtime so full delegates snapshot is stored
    without hardcoding schema.

    - delegates.del_img_path is always saved with a trailing '/'.
    - del_profile_logs.conf_id is stamped from the selected conference
      (delegates only has conference_id, so it would otherwise stay NULL).
    - del_profile_logs.action is stamped as LOG_ACTION_BEFORE_CROP.
    - del_profile_logs.created_at/updated_at are stamped with IST time
      instead of relying on DB server timezone.
    """
    # 1) delegates.del_img_path must end with '/'
    new_path = _ensure_trailing_slash(new_path)
    conn = get_connection(database=conf_schema)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM delegates WHERE delegate_id=%s FOR UPDATE", (delegate_id,))
            old_row = cur.fetchone()
            if not old_row:
                raise ValueError(f"delegate_id {delegate_id} not found in {conf_schema}.delegates")

            # 1) Backup full old row into del_profile_logs (matching cols only)
            #     + stamp conf_id / action / IST timestamps (not present in delegates).
            log_cols = _table_columns(conn, 'del_profile_logs')
            insert_cols = [c for c in old_row.keys() if c in log_cols and c != 'log_id']
            vals = [old_row[c] for c in insert_cols]

            def _set_log_col(name, value):
                if name not in log_cols:
                    return
                if name in insert_cols:
                    vals[insert_cols.index(name)] = value
                else:
                    insert_cols.append(name)
                    vals.append(value)

            # conf_id comes from the selected conference (delegates has no conf_id)
            if conf_id is not None:
                try:
                    _conf_id_val = int(conf_id)
                except (TypeError, ValueError):
                    _conf_id_val = conf_id
                _set_log_col('conf_id', _conf_id_val)
            # action describing why this backup was taken
            _set_log_col('action', LOG_ACTION_BEFORE_CROP)
            # created_at/updated_at in IST (don't copy delegate's old timestamps,
            # don't rely on DB server timezone)
            _ist_now = _ist_now_naive()
            _set_log_col('created_at', _ist_now)
            _set_log_col('updated_at', _ist_now)

            if not insert_cols:
                raise ValueError('del_profile_logs has no matching columns with delegates')
            placeholders = ', '.join(['%s'] * len(insert_cols))
            col_list = ', '.join([f"`{c}`" for c in insert_cols])
            cur.execute(
                f"INSERT INTO del_profile_logs ({col_list}) VALUES ({placeholders})",
                vals,
            )

            # 2) Update delegates to new S3 photo (only existing columns)
            del_cols = _table_columns(conn, 'delegates')
            sets, params = [], []
            if 'del_img_path' in del_cols:
                sets.append("del_img_path=%s")
                params.append(new_path)
            if 'del_img_filename' in del_cols:
                sets.append("del_img_filename=%s")
                params.append(new_filename)
            if 'updated_at' in del_cols:
                sets.append("updated_at=NOW()")
            if 'is_photo_upload_at' in del_cols:
                sets.append("is_photo_upload_at=NOW()")
            if 'profile_photo_status' in del_cols:
                sets.append("profile_photo_status=%s")
                params.append(1)
            if not sets:
                raise ValueError('delegates has no photo columns to update')
            params.append(delegate_id)
            cur.execute(f"UPDATE delegates SET {', '.join(sets)} WHERE delegate_id=%s", params)

        conn.commit()
        return old_row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _upload_one(conf_schema, did, conf_key, force=False, csv_w=None, logger=None, conf_id=None):
    """Upload a single delegate of ONE conference: S3 put, then backup old row
    + update delegates in one DB txn on that conference's schema. On full
    success flips entry status to GOOD and syncs that conference's dashboard
    counters. Returns (result, message) with result in UPLOADED / SKIPPED /
    FAILED. Failed items keep their crop status.
    """
    entry = conf_state.get_entry(conf_schema, did)
    if not entry:
        return 'SKIPPED', 'photo not loaded'
    # AUTO_UPDATED (auto crop) and MANUAL_UPDATED (manual crop) are eligible.
    # Once GOOD, never re-uploaded.
    if entry.get('status') not in ('AUTO_UPDATED', 'MANUAL_UPDATED', 'OK'):
        return 'SKIPPED', f"status {entry.get('status')} not eligible"
    preview = entry.get('preview_path')
    if not preview or not os.path.exists(preview):
        entry['s3_status'] = 'FAILED'
        entry['s3_error'] = 'preview file missing'
        if csv_w is not None:
            try:
                csv_w.writerow([did, '', '', '', '', '', '', 'FAILED: preview file missing'])
            except Exception:
                pass
        return 'FAILED', 'preview file missing'
    if entry.get('s3_status') == 'UPLOADED' and not force:
        return 'SKIPPED', 'already uploaded'

    rec = entry.get('record', {}) or {}
    full_name = rec.get('full_name', '') or f"Delegate-{did}"
    old_path = rec.get('del_img_path', '')
    old_file = rec.get('del_img_filename', '')
    try:
        s3_key, new_filename = s3_service.build_s3_key(conf_key, did, full_name)
        s3_url = s3_service.upload_file(preview, s3_key)
        new_path = _ensure_trailing_slash(s3_service.s3_base_path(conf_key))

        # DB: backup old row + point to new S3 (one txn, commit together)
        _backup_and_update(conf_schema, did, new_path, new_filename, conf_id=conf_id)

        entry['s3_key'] = s3_key
        entry['s3_url'] = s3_url
        entry['s3_status'] = 'UPLOADED'
        entry['s3_error'] = ''
        entry['uploaded_at'] = datetime.now().isoformat()
        # Flip to GOOD only after S3 + DB commit both succeed
        entry['status'] = 'GOOD'
        entry['detail'] = 'uploaded to S3'
        # Keep a copy in good/ so restart-restore finds it as GOOD even
        # without the DB S3 check (preview/ scan = AUTO_UPDATED).
        try:
            _pv_dir, _gd_dir, _od_dir = conf_state.conf_dirs(conf_key)
            _good_dest = os.path.join(_gd_dir, f"{did}.jpg")
            if preview and os.path.exists(preview) and os.path.abspath(preview) != os.path.abspath(_good_dest):
                import shutil
                shutil.copyfile(preview, _good_dest)
                entry['preview_path'] = _good_dest
        except Exception:
            pass
        # Refresh in-memory record so UI shows new filename
        rec['del_img_path'] = new_path
        rec['del_img_filename'] = new_filename
        try:
            job = conf_state.get_processing_job(conf_schema)
            job['status']['good'] = job['status'].get('good', 0) + 1
            job['status']['auto_updated'] = max(0, job['status'].get('auto_updated', 0) - 1)
            # MANUAL_UPDATED counter is refreshed via recount below
            conf_state.recount_processing_status(conf_schema)
        except Exception:
            pass
        if csv_w is not None:
            try:
                csv_w.writerow([did, old_path, old_file, new_path, new_filename, s3_key, s3_url, 'UPLOADED'])
            except Exception:
                pass
        return 'UPLOADED', s3_url
    except Exception as e:
        if logger is not None:
            try:
                logger.exception(f"S3 upload/DB update failed for {did}: {e}")
            except Exception:
                pass
        entry['s3_status'] = 'FAILED'
        entry['s3_error'] = str(e)[:500]
        if csv_w is not None:
            try:
                csv_w.writerow([did, old_path, old_file, '', '', '', '', f'FAILED: {e}'])
            except Exception:
                pass
        return 'FAILED', str(e)[:500]


def _upload_worker(app, conf_schema, delegate_ids, conf_key, force=False, conf_id=None):
    job = conf_state.get_upload_job(conf_schema)
    ust = job['status']
    with app.app_context():
        try:
            ust.update(is_running=True, processed=0, uploaded=0,
                       failed=0, skipped=0, last_error='',
                       started_at=datetime.now().isoformat(),
                       finished_at='')
            ust['total'] = len(delegate_ids)
            ts_backup = datetime.now().strftime('%Y%m%d_%H%M%S')
            csv_path = os.path.join(BACKUP_DIR, f's3_update_{conf_key}_{ts_backup}.csv')
            csv_f = open(csv_path, 'w', newline='')
            csv_w = csv.writer(csv_f)
            csv_w.writerow(['delegate_id', 'old_path', 'old_filename', 'new_path', 'new_filename', 's3_key', 's3_url', 'result'])

            for did in delegate_ids:
                result, msg = _upload_one(conf_schema, did, conf_key, force, csv_w=csv_w, logger=app.logger, conf_id=conf_id)
                if result == 'UPLOADED':
                    ust['uploaded'] += 1
                elif result == 'FAILED':
                    ust['failed'] += 1
                    ust['last_error'] = msg
                else:
                    ust['skipped'] += 1
                ust['processed'] += 1
            try:
                csv_f.close()
            except Exception:
                pass
        except Exception as e:
            ust['last_error'] = str(e)[:500]
        finally:
            ust['is_running'] = False
            ust['finished_at'] = datetime.now().isoformat()


@upload_bp.route('/bulk', methods=['POST'])
def bulk_upload():
    """Start bulk S3 upload for checked (selected) photos of THIS conference.
    JSON {delegate_ids,q,force}. Falls back to {status,q} filter collection
    within this conference when no explicit ids given."""
    data = request.get_json(silent=True) or {}
    status_filter = data.get('status') or request.form.get('status', 'auto_updated')
    search = data.get('q', request.form.get('q', ''))
    force = bool(data.get('force', False))

    sel = session.get('selected_conference')
    if not sel:
        return jsonify({'error': 'No conference selected'}), 400
    conf_key = sel.get('conf_key')
    conf_schema = sel.get('conf_schema')
    conf_id = sel.get('conf_id')
    if not conf_key or not conf_schema:
        return jsonify({'error': 'Selected conference has no conf_key/conf_schema'}), 400
    if not Config.S3_BUCKET:
        return jsonify({'error': 'S3_BUCKET is not configured in .env'}), 400
    job = conf_state.get_upload_job(conf_schema)
    thread = job.get('thread')
    if thread and thread.is_alive():
        return jsonify({'error': f"Upload already running for {sel.get('conf_name','')}", 'status': job['status']}), 409

    delegate_ids = []
    raw_ids = data.get('delegate_ids')
    if isinstance(raw_ids, list) and raw_ids:
        seen = set()
        for _v in raw_ids:
            try:
                _did = int(_v)
            except (TypeError, ValueError):
                continue
            if _did and _did not in seen:
                seen.add(_did)
                delegate_ids.append(_did)
    else:
        delegate_ids = _collect_ids(conf_schema, status_filter, search)
    if not delegate_ids:
        return jsonify({'error': 'No photos selected — tick at least one S3 checkbox'}), 404

    app = current_app._get_current_object()
    thread = threading.Thread(
        target=_upload_worker, args=(app, conf_schema, delegate_ids, conf_key, force, conf_id), daemon=True)
    job['thread'] = thread
    thread.start()
    job['status']['total'] = len(delegate_ids)
    return jsonify({'started': True, 'total': len(delegate_ids), 'conf_key': conf_key})


@upload_bp.route('/test')
def test_s3_connection():
    """One-click S3 diagnosis (server config check, no conference needed).
    Returns per-step results: config -> HeadBucket -> PutObject probe,
    with the likely fix for each failure. Never exposes secret values."""
    try:
        result = s3_service.test_connection()
        return jsonify(result), (200 if result.get('ok') else 503)
    except Exception as e:
        current_app.logger.exception(f"S3 test failed: {e}")
        return jsonify({'ok': False, 'bucket': Config.S3_BUCKET or '',
                        'region': Config.AWS_REGION or '',
                        'steps': [{'name': 'test', 'ok': False,
                                   'detail': str(e)[:300]}]}), 500


@upload_bp.route('/status')
def upload_status_endpoint():
    s3_ok = bool(Config.S3_BUCKET)
    sel = session.get('selected_conference') or {}
    conf_schema = sel.get('conf_schema')
    if conf_schema:
        data = dict(conf_state.get_upload_job(conf_schema)['status'])
    else:
        data = dict(conf_state.UPLOAD_STATUS_DEFAULTS)
    data['s3_configured'] = s3_ok
    data['dry_run'] = bool(Config.S3_DRY_RUN)
    return jsonify(data)


@upload_bp.route('/single', methods=['POST'])
def single_upload():
    """Upload one photo box to S3 (synchronous). JSON {delegate_id}.
    AUTO_UPDATED and MANUAL_UPDATED entries of THIS conference are eligible;
    GOOD items are never re-uploaded (re-crop first to make them MANUAL_UPDATED
    again). On full success the entry flips to GOOD."""
    data = request.get_json(silent=True) or {}
    try:
        did = int(data.get('delegate_id') or request.form.get('delegate_id') or 0)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid delegate_id'}), 400
    if not did:
        return jsonify({'success': False, 'error': 'delegate_id required'}), 400

    sel = session.get('selected_conference')
    if not sel:
        return jsonify({'success': False, 'error': 'No conference selected'}), 400
    conf_key = sel.get('conf_key')
    conf_schema = sel.get('conf_schema')
    conf_id = sel.get('conf_id')
    if not conf_key or not conf_schema:
        return jsonify({'success': False, 'error': 'Selected conference has no conf_key/conf_schema'}), 400
    if not Config.S3_BUCKET:
        return jsonify({'success': False, 'error': 'S3_BUCKET is not configured in .env'}), 400
    if conf_state.get_entry(conf_schema, did) is None:
        return jsonify({'success': False, 'error': 'Photo not loaded — reload photos page'}), 404

    result, msg = _upload_one(conf_schema, did, conf_key, force=False,
                              csv_w=None, logger=current_app.logger, conf_id=conf_id)
    if result == 'UPLOADED':
        return jsonify({'success': True, 's3_url': msg, 'new_status': 'GOOD',
                        'delegate_id': did})
    if result == 'SKIPPED':
        return jsonify({'success': False, 'error': msg, 'delegate_id': did}), 409
    return jsonify({'success': False, 'error': msg, 'delegate_id': did}), 500
