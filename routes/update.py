import os
import shutil
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify, session
from db.database import get_connection
from config import Config
from routes import state as conf_state
from routes.state import processing_data

update_bp = Blueprint('update', __name__, url_prefix='/update')

# Ensure processed directory exists
PROCESSED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'storage', 'processed'))
os.makedirs(PROCESSED_DIR, exist_ok=True)

def _scoped_or_all():
    sel = session.get('selected_conference') or {}
    conf_schema = sel.get('conf_schema')
    if conf_schema:
        return conf_state.scoped_entries(conf_schema)
    return list(processing_data.values())

@update_bp.route('/review')
def review():
    # Gather stats for display (this conference when selected)
    entries = _scoped_or_all()
    total = len(entries)
    approved = [d for d in entries if d['status'] == 'OK']
    rejected = [d for d in entries if d['status'] == 'REJECTED']
    needs_review = [d for d in entries if d['status'] in ('NO_FACE', 'MULTIPLE_FACES', 'LOW_CONFIDENCE', 'IMAGE_DOWNLOAD_ERROR')]
    return render_template('review.html', total=total, approved=approved, rejected=rejected, needs_review=needs_review)

@update_bp.route('/confirm', methods=['POST'])
def confirm_bulk_update():
    from datetime import datetime
    entries = _scoped_or_all()
    # Perform bulk update within a transaction
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Create backup CSV
            backup_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'storage', 'backup'))
            os.makedirs(backup_path, exist_ok=True)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_file = os.path.join(backup_path, f'photo_update_{timestamp}.csv')
            with open(backup_file, 'w') as bf:
                bf.write('delegate_id,del_img_path,del_img_filename\n')
                for record in entries:
                    if record['status'] != 'OK':
                        continue
                    orig = record['record']
                    bf.write(f"{orig['delegate_id']},{orig['del_img_path']},{orig['del_img_filename']}\n")
            # Update each approved record
            for record in entries:
                if record['status'] != 'OK':
                    continue
                orig = record['record']
                delegate_id = orig['delegate_id']
                # Move processed image to final folder (for demo we keep same filename)
                src_path = record.get('preview_path')
                if not src_path:
                    continue
                new_filename = f"{delegate_id}.jpg"
                dest_path = os.path.join(PROCESSED_DIR, new_filename)
                shutil.move(src_path, dest_path)
                # Build new DB values (assuming same base URL, just change path/filename)
                new_path = 'processed/'  # adjust as needed for actual storage
                update_sql = """
                    UPDATE delegates
                    SET del_img_path = %s,
                        del_img_filename = %s,
                        updated_at = NOW()
                    WHERE delegate_id = %s
                """
                cur.execute(update_sql, (new_path, new_filename, delegate_id))
        conn.commit()
        flash('Bulk update successful.', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Error during bulk update: {e}', 'danger')
    finally:
        conn.close()
    return redirect(url_for('dashboard.dashboard'))
