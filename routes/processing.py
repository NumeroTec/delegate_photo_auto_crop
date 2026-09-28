from flask import Blueprint, render_template, request, jsonify, redirect, url_for, current_app, session
import threading
import os
from services.photo_service import get_all_delegates, build_photo_url, download_image
from services.face_detector import detect_face
from services.crop_service import generate_passport_crop
from routes import state as conf_state

processing_bp = Blueprint('processing', __name__, url_prefix='/processing')

# Shared in-memory store lives in routes/state.py, keyed by
# (conf_schema, delegate_id) so conferences never overwrite each other.
processing_data = conf_state.processing_data

# Legacy background worker (kept for backwards compat). Scoped to the
# requesting session's conference via routes/state.py.

def processing_worker(conf_schema=None, conf_key=None):
    """Background worker that processes delegates in batches.
    Updates keyed `processing_data` and that conference's job status only.
    """
    job = conf_state.get_processing_job(conf_schema)
    st = job['status']
    batch_size = current_app.config.get('BATCH_SIZE', 50)
    delegates = get_all_delegates()
    total = len(delegates)
    conf_state.reset_status(st)
    st['total'] = total
    for i in range(0, total, batch_size):
        batch = delegates[i:i+batch_size]
        for record in batch:
            delegate_id = record['delegate_id']
            # Build URL and download image
            url = build_photo_url(record['del_img_path'], record['del_img_filename'])
            local_path = download_image(url, delegate_id, conf_key=conf_key)
            if not local_path:
                status = 'IMAGE_DOWNLOAD_ERROR'
                conf_state.set_entry(conf_schema, delegate_id, {
                    'original_url': url,
                    'original_path': None,
                    'preview_path': None,
                    'status': status,
                    'confidence': None,
                    'bbox': None,
                })
                st['errors'] += 1
                st['processed'] += 1
                continue
            # Detect face
            detection = detect_face(local_path)
            status = detection['status']
            confidence = detection.get('confidence')
            bbox = detection.get('bbox')
            preview_path = None
            if status == 'OK':
                # Generate passport crop
                preview_path = generate_passport_crop(local_path, bbox, delegate_id, conf_key=conf_key)
                st['auto_approved'] += 1
            elif status in ('NO_FACE', 'MULTIPLE_FACES', 'LOW_CONFIDENCE'):
                st['needs_review'] += 1
            else:
                st['errors'] += 1
            st['processed'] += 1
            conf_state.set_entry(conf_schema, delegate_id, {
                'original_url': url,
                'original_path': local_path,
                'preview_path': preview_path,
                'status': status,
                'confidence': confidence,
                'bbox': bbox,
                'record': record,
                's3_status': 'PENDING',
                's3_key': None,
                's3_url': None,
                's3_error': '',
                'uploaded_at': None,
            })
        # After each batch we could sleep a bit (omitted for demo)

@processing_bp.route('/')
def processing_home():
    # Show simple grid view with pagination parameters
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    sel = session.get('selected_conference') or {}
    conf_schema = sel.get('conf_schema')
    # Ensure we have processed data for THIS conference; if not, redirect to start
    if not conf_schema or not conf_state.scoped_entries(conf_schema):
        return redirect(url_for('dashboard.start_processing'))
    ids = [k[1] for k in list(processing_data.keys()) if k[0] == conf_schema]
    total = len(ids)
    start = (page - 1) * per_page
    end = start + per_page
    page_ids = ids[start:end]
    items = [conf_state.get_entry(conf_schema, did) for did in page_ids]
    return render_template('processing_grid.html', items=items, page=page, per_page=per_page, total=total)

@processing_bp.route('/status')
def status_endpoint():
    # Return JSON status for THIS conference (for UI polling)
    sel = session.get('selected_conference') or {}
    conf_schema = sel.get('conf_schema')
    if conf_schema:
        return jsonify(conf_state.get_processing_job(conf_schema)['status'])
    return jsonify(dict(conf_state.STATUS_DEFAULTS))
