"""Per-conference isolated server state (Option 2: multi-conference parallel use).

Every browser session selects its own conference (session['selected_conference']
with conf_schema + conf_key). All server-side state below is namespaced by
conf_schema so two systems can process / view / upload DIFFERENT conferences
at the same time without overwriting each other:

  processing_data : {(conf_schema, delegate_id): entry}
  processing jobs : {conf_schema: {'status': {...}, 'thread': Thread|None, 'is_processing': bool}}
  upload jobs     : {conf_schema: {'thread': ..., 'status': {...}}}

On-disk images are namespaced too: storage/<conf_key>/preview|good|original/.
Legacy flat storage/<preview|good|original>/ files are still read as a
fallback (attributed to the requesting conference) but all NEW writes go to
the per-conference folders.

This module imports nothing from routes/* so it can be safely imported
anywhere without circular imports.
"""
import os

# ---------------------------------------------------------------------------
# In-memory photo store: (conf_schema, delegate_id) -> entry dict.
# Entry dicts also carry 'conf_schema' and 'conf_key' for convenience.
# ---------------------------------------------------------------------------
processing_data = {}


def data_key(conf_schema, delegate_id):
    return (conf_schema, int(delegate_id))


def get_entry(conf_schema, delegate_id):
    if not conf_schema:
        return None
    try:
        return processing_data.get(data_key(conf_schema, delegate_id))
    except (TypeError, ValueError):
        return None


def set_entry(conf_schema, delegate_id, entry):
    entry = entry or {}
    entry['conf_schema'] = conf_schema
    processing_data[data_key(conf_schema, delegate_id)] = entry
    return entry


def scoped_entries(conf_schema):
    """All entry dicts belonging to one conference."""
    if not conf_schema:
        return []
    return [e for (s, _d), e in list(processing_data.items()) if s == conf_schema]


def clear_conference_data(conf_schema):
    """Drop in-memory entries of ONE conference only (never touches others)."""
    if not conf_schema:
        return
    for k in [k for k in list(processing_data.keys()) if k[0] == conf_schema]:
        processing_data.pop(k, None)


# ---------------------------------------------------------------------------
# Processing jobs (one worker thread + counters per conference)
# ---------------------------------------------------------------------------
STATUS_DEFAULTS = {
    'total': 0,
    'processed': 0,
    'good': 0,              # already passport-like, skipped crop
    'auto_updated': 0,      # auto cropped to 354x472
    'manual_updated': 0,    # manually cropped to 354x472 via Crop button
    'failed': 0,            # crop failed / face not detected / uncrop
    'auto_approved': 0,     # legacy alias of auto_updated
    'needs_review': 0,
    'no_face': 0,
    'multiple_faces': 0,
    'low_confidence': 0,
    'errors': 0,
    'rejected': 0,
}

_processing_jobs = {}


def reset_status(status=None):
    st = dict(STATUS_DEFAULTS)
    if status is not None:
        status.clear()
        status.update(st)
        return status
    return st


def get_processing_job(conf_schema):
    """Return (creating) the processing job dict for a conference."""
    job = _processing_jobs.get(conf_schema)
    if job is None:
        job = {'status': reset_status(), 'thread': None, 'is_processing': False}
        _processing_jobs[conf_schema] = job
    return job


def recount_processing_status(conf_schema):
    """Recompute good/auto/manual/failed counters from scoped entries."""
    job = get_processing_job(conf_schema)
    st = job['status']
    entries = scoped_entries(conf_schema)
    st['good'] = sum(1 for d in entries if d.get('status') == 'GOOD')
    st['auto_updated'] = sum(1 for d in entries
                             if d.get('status') in ('AUTO_UPDATED', 'OK'))
    st['manual_updated'] = sum(1 for d in entries if d.get('status') == 'MANUAL_UPDATED')
    st['failed'] = sum(1 for d in entries if d.get('status') == 'FAILED')
    return st


# ---------------------------------------------------------------------------
# Upload jobs (one worker thread + progress per conference)
# ---------------------------------------------------------------------------
UPLOAD_STATUS_DEFAULTS = {
    'total': 0,
    'processed': 0,
    'uploaded': 0,
    'failed': 0,
    'skipped': 0,
    'is_running': False,
    'last_error': '',
    'started_at': '',
    'finished_at': '',
}

_upload_jobs = {}


def get_upload_job(conf_schema):
    job = _upload_jobs.get(conf_schema)
    if job is None:
        job = {'thread': None, 'status': dict(UPLOAD_STATUS_DEFAULTS)}
        _upload_jobs[conf_schema] = job
    return job


# ---------------------------------------------------------------------------
# Storage directories: storage/<conf_key>/preview|good|original (+ legacy flat)
# ---------------------------------------------------------------------------
STORAGE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'storage'))


def _safe_conf_key(conf_key):
    """Sanitize conf_key for use as a folder name (fallback: 'default')."""
    import re
    key = (conf_key or '').strip()
    key = re.sub(r'[^A-Za-z0-9_\-]', '', key)
    return key or 'default'


def conf_dirs(conf_key):
    """Return (preview_dir, good_dir, original_dir) for a conference, creating them."""
    key = _safe_conf_key(conf_key)
    preview = os.path.join(STORAGE_ROOT, key, 'preview')
    good = os.path.join(STORAGE_ROOT, key, 'good')
    original = os.path.join(STORAGE_ROOT, key, 'original')
    os.makedirs(preview, exist_ok=True)
    os.makedirs(good, exist_ok=True)
    os.makedirs(original, exist_ok=True)
    return preview, good, original


def legacy_dirs():
    """Legacy flat storage dirs (read fallback only)."""
    return (os.path.join(STORAGE_ROOT, 'preview'),
            os.path.join(STORAGE_ROOT, 'good'),
            os.path.join(STORAGE_ROOT, 'original'))
