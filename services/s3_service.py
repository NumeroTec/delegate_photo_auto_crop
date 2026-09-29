"""AWS S3 helpers for cropped photo upload.

URL format:
    https://<bucket>.s3.<region>.amazonaws.com/delegate_photo/<conf_key>/<delegate_id>-<FullName>-<Ymd>-<His>.jpg
"""
import os
import re
from datetime import datetime

from config import Config


def sanitize_name(full_name, max_len=50):
    """Sanitize full name for S3 filename: spaces->-, keep alnum+-, trim."""
    name = (full_name or '').strip()
    name = re.sub(r'\s+', '-', name)
    name = re.sub(r'[^A-Za-z0-9\-]', '', name)
    name = re.sub(r'-+', '-', name).strip('-')
    if not name:
        return ''
    return name[:max_len]


def build_s3_key(conf_key, delegate_id, full_name, timestamp=None, prefix=None):
    """Build S3 key: delegate_photo/<conf_key>/<delegate_id>-<Name>-<Ymd>-<His>.jpg"""
    prefix = (prefix or Config.S3_PREFIX or 'delegate_photo').strip('/')
    safe_name = sanitize_name(full_name) or f'Delegate-{delegate_id}'
    ts = timestamp or datetime.now().strftime('%Y%m%d-%H%M%S')
    filename = f"{delegate_id}-{safe_name}-{ts}.jpg"
    return f"{prefix}/{conf_key}/{filename}", filename


def s3_public_url(s3_key):
    bucket = Config.S3_BUCKET
    region = Config.AWS_REGION or 'ap-southeast-1'
    return f"https://{bucket}.s3.{region}.amazonaws.com/{s3_key}"


def s3_base_path(conf_key):
    """del_img_path value stored in DB so build_photo_url() resolves to S3."""
    bucket = Config.S3_BUCKET
    region = Config.AWS_REGION or 'ap-southeast-1'
    prefix = (Config.S3_PREFIX or 'delegate_photo').strip('/')
    return f"https://{bucket}.s3.{region}.amazonaws.com/{prefix}/{conf_key}/"


def get_s3_client():
    import boto3
    from botocore.config import Config as BotoConfig
    return boto3.client(
        's3',
        region_name=Config.AWS_REGION or 'ap-southeast-1',
        aws_access_key_id=Config.AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY or None,
        config=BotoConfig(connect_timeout=8, read_timeout=15, retries={'max_attempts': 1}),
    )


def upload_file(local_path, s3_key):
    """Upload local JPEG to S3. Returns public URL. Raises on failure."""
    if Config.S3_DRY_RUN:
        return s3_public_url(s3_key)
    if not Config.S3_BUCKET:
        raise ValueError('S3_BUCKET is not configured (.env)')
    client = get_s3_client()
    client.upload_file(
        local_path, Config.S3_BUCKET, s3_key,
        ExtraArgs={'ContentType': 'image/jpeg'},
    )
    return s3_public_url(s3_key)


def s3_configured():
    return bool(Config.S3_BUCKET)


def _code_of(exc):
    """Extract AWS error code (e.g. 'AccessDenied') from a botocore exception."""
    try:
        return exc.response.get('Error', {}).get('Code', '')
    except Exception:
        return ''


def test_connection():
    """Step-by-step S3 diagnosis: credentials -> bucket reachable ->
    PutObject probe. Returns dict with per-step ok/info/error, safe to show in UI
    (never includes secret values)."""
    import time
    steps = []
    bucket = Config.S3_BUCKET or ''
    region = Config.AWS_REGION or 'ap-southeast-1'
    prefix = (Config.S3_PREFIX or 'delegate_photo').strip('/')

    def step(name, ok, detail=''):
        steps.append({'name': name, 'ok': bool(ok), 'detail': str(detail)[:300]})
        return ok

    # 1) config present (keys are only checked for presence, never echoed)
    if not bucket:
        step('config', False, 'S3_BUCKET is empty in .env')
        return {'ok': False, 'bucket': bucket, 'region': region, 'steps': steps}
    if not Config.AWS_ACCESS_KEY_ID or not Config.AWS_SECRET_ACCESS_KEY:
        step('config', False, 'S3_BUCKET is set but AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY is missing in .env')
        return {'ok': False, 'bucket': bucket, 'region': region, 'steps': steps}
    step('config', True, f'bucket={bucket} region={region} prefix={prefix} (keys present, values hidden)')

    # 2) bucket reachable with these credentials
    try:
        client = get_s3_client()
        client.head_bucket(Bucket=bucket)
        step('head_bucket', True, f'bucket "{bucket}" reachable with these credentials')
    except Exception as e:
        code = _code_of(e)
        if code in ('403', 'AccessDenied', 'Forbidden'):
            step('head_bucket', False,
                 'AccessDenied on HeadBucket: key cannot even see the bucket. '
                 'Causes: wrong bucket name, key from another AWS account with no grant, '
                 'or bucket policy denies this IAM user. Fix IAM/bucket policy (need s3:ListBucket).')
        elif code in ('404', 'NoSuchBucket', 'NotFound'):
            step('head_bucket', False,
                 f'NoSuchBucket: "{bucket}" does not exist (typo? wrong account?).')
        elif code in ('InvalidAccessKeyId',):
            step('head_bucket', False, 'InvalidAccessKeyId: key ID is wrong or deleted. Check .env.')
        elif code in ('SignatureDoesNotMatch',):
            step('head_bucket', False, 'SignatureDoesNotMatch: secret key is wrong. Check .env.')
        elif code in ('301', 'PermanentRedirect', 'AuthorizationHeaderMalformed', 'BadRequest'):
            step('head_bucket', False,
                 f'{code or "redirect"}: bucket is probably in a different region than AWS_REGION={region}. '
                 'Set AWS_REGION to the bucket\'s real region (check S3 console).')
        else:
            step('head_bucket', False, f'{code or type(e).__name__}: {e}'.strip()[:300])
        return {'ok': False, 'bucket': bucket, 'region': region, 'steps': steps}

    # 3) PutObject probe (same call pattern as real photo upload)
    probe_key = f'{prefix}/_healthcheck_{int(time.time())}.txt'
    try:
        client.put_object(Bucket=bucket, Key=probe_key, Body=b'healthcheck',
                          ContentType='text/plain')
        try:
            client.delete_object(Bucket=bucket, Key=probe_key)
        except Exception:
            pass
        step('put_object', True, f'probe write+delete OK at {probe_key}')
        return {'ok': True, 'bucket': bucket, 'region': region, 'steps': steps}
    except Exception as e:
        code = _code_of(e)
        if code in ('403', 'AccessDenied'):
            step('put_object', False,
                 'AccessDenied on PutObject (your exact error): credentials can SEE the bucket '
                 'but may not WRITE. Fix: grant s3:PutObject (+s3:DeleteObject for cleanup) on '
                 f'arn:aws:s3:::{bucket}/{prefix}/* to this IAM user. '
                 'If the bucket enforces SSE-KMS via policy, also grant kms:GenerateDataKey, '
                 'or relax the bucket policy. Check bucket Ownership settings too.')
        else:
            step('put_object', False, f'{code or type(e).__name__}: {e}'.strip()[:300])
        return {'ok': False, 'bucket': bucket, 'region': region, 'steps': steps}
