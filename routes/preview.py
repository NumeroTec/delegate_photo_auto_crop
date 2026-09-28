from flask import Blueprint, render_template, abort, current_app, send_from_directory, url_for, session
import os
from routes import state as conf_state

preview_bp = Blueprint('preview', __name__, url_prefix='/preview')

@preview_bp.route('/<int:delegate_id>')
def preview(delegate_id):
    sel = session.get('selected_conference') or {}
    conf_schema = sel.get('conf_schema')
    data = conf_state.get_entry(conf_schema, delegate_id) if conf_schema else None
    if not data:
        abort(404)
    # Paths for serving images
    original_url = data['original_url']
    preview_path = data.get('preview_path')
    # If preview_path exists, serve from storage
    preview_url = None
    if preview_path:
        # Serve via static route - we will map storage directory via send_from_directory
        preview_url = url_for('preview.serve_preview_image', filename=os.path.basename(preview_path)) if preview_path else None
    return render_template('preview.html', delegate=data, original_url=original_url, preview_url=preview_url)
