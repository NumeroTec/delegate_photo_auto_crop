"""Per-conference photo history (del_profile_logs) preview + restore.

Shown AFTER a conference is set (session['selected_conference']).
Lists del_profile_logs rows for the selected conf_schema with:
  - log (old/backed-up) photo preview
  - current delegates photo preview
  - per-row Restore button (restores delegates.del_img_path/filename
    to the log row values, backing up current state first).
"""
import os

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, jsonify)

from db.database import get_connection
from services.photo_service import build_photo_url

logs_bp = Blueprint('logs', __name__, url_prefix='/logs')


def _ist_now_naive():
    """Current IST (Asia/Kolkata) time as naive datetime for MySQL DATETIME cols."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    except Exception:
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


def _selected():
    return session.get('selected_conference')


def _table_exists(conn, table):
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES LIKE %s", (table,))
        return cur.fetchone() is not None


@logs_bp.route('/')
def logs_list():
    """Log preview list for the selected conference."""
    sel = _selected()
    if not sel:
        flash("Please select a conference first to view logs.", "warning")
        return redirect(url_for('dashboard.dashboard'))
    conf_schema = sel.get('conf_schema')
    if not conf_schema:
        flash("Selected conference has no conf_schema.", "danger")
        return redirect(url_for('dashboard.dashboard'))

    page = max(1, int(request.args.get('page', 1)))
    per_page = int(request.args.get('per_page', 50))
    if per_page not in (25, 50, 100, 200):
        per_page = 50
    search = request.args.get('q', '').strip()

    logs, total, total_pages = [], 0, 1
    try:
        conn = get_connection(database=conf_schema)
        try:
            if not _table_exists(conn, 'del_profile_logs'):
                flash(f"Table del_profile_logs not found in {conf_schema}.", "danger")
                return render_template('logs.html', logs=[], total=0,
                                       page=page, per_page=per_page,
                                       total_pages=1, search=search,
                                       selected_conf=sel)
            with conn.cursor() as cur:
                where, params = "", []
                if search:
                    where = ("WHERE CAST(delegate_id AS CHAR) LIKE %s "
                             "OR full_name LIKE %s OR delegate_no LIKE %s "
                             "OR del_img_filename LIKE %s")
                    like = f"%{search}%"
                    params = [like, like, like, like]
                cur.execute(f"SELECT COUNT(*) AS c FROM del_profile_logs {where}", params)
                total = (cur.fetchone() or {}).get('c', 0) or 0
                total_pages = max(1, (total + per_page - 1) // per_page)
                page = min(page, total_pages)
                offset = (page - 1) * per_page
                cur.execute(
                    f"""SELECT log_id, conf_id, delegate_id, delegate_no, full_name,
                               del_img_path, del_img_filename, created_at, updated_at, action
                        FROM del_profile_logs {where}
                        ORDER BY log_id DESC LIMIT %s OFFSET %s""",
                    params + [per_page, offset],
                )
                logs = cur.fetchall() or []

                # Batch-fetch current delegates photos for comparison
                current_map = {}
                ids = [r['delegate_id'] for r in logs if r.get('delegate_id')]
                if ids:
                    placeholders = ', '.join(['%s'] * len(ids))
                    cur.execute(
                        f"""SELECT delegate_id, full_name, del_img_path, del_img_filename, updated_at
                            FROM delegates WHERE delegate_id IN ({placeholders})""",
                        ids,
                    )
                    for row in cur.fetchall():
                        current_map[row['delegate_id']] = row
        finally:
            conn.close()
    except Exception as e:
        flash(f"Error loading logs from {conf_schema}: {e}", "danger")
        return render_template('logs.html', logs=[], total=0,
                               page=page, per_page=per_page,
                               total_pages=1, search=search,
                               selected_conf=sel)

    # Attach preview URLs (old/log vs current)
    for row in logs:
        try:
            row['log_photo_url'] = build_photo_url(row.get('del_img_path'),
                                                   row.get('del_img_filename'))
        except Exception:
            row['log_photo_url'] = None
        cur_row = current_map.get(row.get('delegate_id')) if row.get('delegate_id') else None
        row['current'] = cur_row
        try:
            row['current_photo_url'] = (build_photo_url(cur_row.get('del_img_path'),
                                                        cur_row.get('del_img_filename'))
                                        if cur_row else None)
        except Exception:
            row['current_photo_url'] = None

    return render_template('logs.html', logs=logs, total=total,
                           page=page, per_page=per_page,
                           total_pages=total_pages, search=search,
                           selected_conf=sel)


@logs_bp.route('/restore/<int:log_id>', methods=['POST'])
def restore_log(log_id):
    """Restore delegates photo to the backed-up values in one log row.

    Safety: current delegates snapshot is inserted into del_profile_logs
    with action='PRE_RESTORE' before the UPDATE, in the same transaction.
    """
    sel = _selected()
    if not sel:
        if request.is_json:
            return jsonify({'success': False, 'error': 'No conference selected'}), 400
        flash("Please select a conference first.", "warning")
        return redirect(url_for('dashboard.dashboard'))
    conf_schema = sel.get('conf_schema')
    if not conf_schema:
        msg = 'Selected conference has no conf_schema'
        if request.is_json:
            return jsonify({'success': False, 'error': msg}), 400
        flash(msg, "danger")
        return redirect(url_for('logs.logs_list'))

    conn = get_connection(database=conf_schema)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM del_profile_logs WHERE log_id=%s", (log_id,))
            log_row = cur.fetchone()
            if not log_row:
                raise ValueError(f"log_id {log_id} not found")
            delegate_id = log_row.get('delegate_id')
            if not delegate_id:
                raise ValueError("Log row has no delegate_id")
            cur.execute("SELECT * FROM delegates WHERE delegate_id=%s FOR UPDATE",
                        (delegate_id,))
            cur_row = cur.fetchone()
            if not cur_row:
                raise ValueError(f"delegate_id {delegate_id} not found in delegates")

            # 1) Backup CURRENT state into logs (matching cols only)
            cur.execute("SHOW COLUMNS FROM del_profile_logs")
            log_cols = [r['Field'] for r in cur.fetchall()]
            insert_cols = [c for c in cur_row.keys() if c in log_cols and c != 'log_id']
            if insert_cols:
                placeholders = ', '.join(['%s'] * len(insert_cols))
                col_list = ', '.join([f"`{c}`" for c in insert_cols])
                vals = [cur_row[c] for c in insert_cols]

                def _set_log_col(name, value):
                    if name not in log_cols:
                        return
                    if name in insert_cols:
                        vals[insert_cols.index(name)] = value
                    else:
                        insert_cols.append(name)
                        vals.append(value)
                        nonlocal placeholders, col_list
                        placeholders = ', '.join(['%s'] * len(insert_cols))
                        col_list = ', '.join([f"`{c}`" for c in insert_cols])

                # stamp action if column exists
                if 'action' in insert_cols or 'action' in log_cols:
                    _set_log_col('action', 'PRE_RESTORE')
                # stamp conf_id from selected conference (delegates has no conf_id)
                _conf_id = (sel or {}).get('conf_id')
                if _conf_id is not None:
                    try:
                        _conf_id = int(_conf_id)
                    except (TypeError, ValueError):
                        pass
                    _set_log_col('conf_id', _conf_id)
                # stamp IST time (don't copy delegate's old timestamps,
                # don't rely on DB server timezone)
                _ist_now = _ist_now_naive()
                _set_log_col('created_at', _ist_now)
                _set_log_col('updated_at', _ist_now)
                cur.execute(f"INSERT INTO del_profile_logs ({col_list}) VALUES ({placeholders})",
                            vals)

            # 2) Restore delegates photo to log values
            # (del_img_path saved with trailing '/')
            cur.execute("SHOW COLUMNS FROM delegates")
            del_cols = [r['Field'] for r in cur.fetchall()]
            sets, params = [], []
            if 'del_img_path' in del_cols:
                sets.append("del_img_path=%s")
                params.append(_ensure_trailing_slash(log_row.get('del_img_path')))
            if 'del_img_filename' in del_cols:
                sets.append("del_img_filename=%s")
                params.append(log_row.get('del_img_filename'))
            if 'updated_at' in del_cols:
                sets.append("updated_at=NOW()")
            if 'profile_photo_status' in del_cols:
                sets.append("profile_photo_status=%s")
                params.append(0)
            if not sets:
                raise ValueError('delegates has no photo columns to restore')
            params.append(delegate_id)
            cur.execute(f"UPDATE delegates SET {', '.join(sets)} WHERE delegate_id=%s", params)
        conn.commit()

        # Sync in-memory keyed entry so photos page reflects restore
        try:
            from routes import state as conf_state
            entry = conf_state.get_entry(conf_schema, delegate_id)
            if entry is not None:
                rec = entry.get('record', {}) or {}
                rec['del_img_path'] = log_row.get('del_img_path')
                rec['del_img_filename'] = log_row.get('del_img_filename')
                entry['record'] = rec
                try:
                    entry['original_url'] = build_photo_url(
                        log_row.get('del_img_path'), log_row.get('del_img_filename'))
                except Exception:
                    pass
                entry['s3_status'] = 'PENDING'
                entry['s3_error'] = ''
        except Exception:
            pass

        msg = (f"Restored delegate {delegate_id} photo from log #{log_id} "
               f"({log_row.get('del_img_filename') or 'old photo'}).")
        if request.is_json or request.args.get('format') == 'json':
            return jsonify({'success': True, 'message': msg, 'delegate_id': delegate_id})
        flash(msg, "success")
    except Exception as e:
        conn.rollback()
        if request.is_json or request.args.get('format') == 'json':
            return jsonify({'success': False, 'error': str(e)[:500]}), 500
        flash(f"Restore failed: {e}", "danger")
    finally:
        conn.close()

    # Preserve list filters on redirect
    return redirect(url_for('logs.logs_list',
                            page=request.args.get('page', 1),
                            per_page=request.args.get('per_page', 50),
                            q=request.args.get('q', '')))
