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
    return boto3.client(
        's3',
        region_name=Config.AWS_REGION or 'ap-southeast-1',
        aws_access_key_id=Config.AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY or None,
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
