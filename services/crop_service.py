import os
import shutil
from PIL import Image
from config import Config

# Directory for processed (preview) images
PREVIEW_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'storage', 'preview'))
GOOD_DIR = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'storage', 'good')))
os.makedirs(PREVIEW_DIR, exist_ok=True)
os.makedirs(GOOD_DIR, exist_ok=True)

def is_passport_photo(image_path):
    """Check if photo is already passport-like: exactly 354x472 and face centered with correct ratio.
    Returns (is_good: bool, reason: str, bbox).
    Step 2: if already passport-like, skip crop and mark as good.
    """
    try:
        img = Image.open(image_path)
        w, h = img.size
        # 1) Exact passport dimensions
        if w != Config.OUTPUT_WIDTH or h != Config.OUTPUT_HEIGHT:
            return False, f"size {w}x{h} != {Config.OUTPUT_WIDTH}x{Config.OUTPUT_HEIGHT}", None
        # 2) Detect face and check face ratio/position is passport-like
        # Import here to avoid circular
        from services.face_detector import detect_face
        detection = detect_face(image_path)
        if detection['status'] != 'OK':
            return False, f"face status {detection['status']}", None
        bbox = detection['bbox']
        x, y, bw, bh = bbox
        # Face height should be approx FACE_TARGET_RATIO * OUTPUT_HEIGHT (212px) with tolerance +-25%
        face_ratio = bh / h
        target = Config.FACE_TARGET_RATIO
        # Top padding check: face top should be around TOP_PADDING_RATIO * out_h (94px)
        top_pad = y / h
        # Allow 30% tolerance for real photos
        if abs(face_ratio - target) > 0.12:  # 0.45 +-0.12 => 0.33-0.57
            return False, f"face_ratio {face_ratio:.2f} != {target}", bbox
        if abs(top_pad - Config.TOP_PADDING_RATIO) > 0.12:
            return False, f"top_pad {top_pad:.2f} != {Config.TOP_PADDING_RATIO}", bbox
        # Centered check: face center x should be near image center
        face_cx = x + bw/2
        if abs(face_cx - w/2) / w > 0.15:
            return False, "face not centered", bbox
        return True, "passport_good", bbox
    except Exception as e:
        return False, f"error {e}", None

def _resolve_dirs(conf_key=None):
    """Return (preview_dir, good_dir); per-conference folders when conf_key given."""
    if conf_key:
        from routes.state import conf_dirs
        preview_dir, good_dir, _od = conf_dirs(conf_key)
        return preview_dir, good_dir
    return PREVIEW_DIR, GOOD_DIR

def copy_as_good(original_path, delegate_id, conf_key=None):
    """Copy already-good passport photo to good folder and preview as same."""
    preview_dir, good_dir = _resolve_dirs(conf_key)
    dest = os.path.join(good_dir, f"{delegate_id}.jpg")
    preview = os.path.join(preview_dir, f"{delegate_id}.jpg")
    try:
        shutil.copyfile(original_path, preview)
        shutil.copyfile(original_path, dest)
    except Exception:
        # Fallback: open and save as jpg
        img = Image.open(original_path)
        img.convert('RGB').save(preview, 'JPEG')
        img.convert('RGB').save(dest, 'JPEG')
    return preview

def _band_has_head(band_img, h, H):
    """Decide whether a crown zone holds forehead/face skin.

    Trigger requires BOTH:
      - skin fraction (YCbCr ellipse) > 0.15 in the zone, and
      - bbox height h >= 13% of image height (distant tiny faces always use
        the normal zoom path — e.g. a beige wall above a small face mimics
        skin fraction but must still zoom).
    Fail-open: False on any error.
    """
    try:
        import numpy as np
        if h < 0.13 * H:
            return False
        rgb = band_img.convert('RGB')
        ycb = np.asarray(rgb.convert('YCbCr'), dtype=np.float32)
        cb, cr = ycb[..., 1], ycb[..., 2]
        skin = (cb >= 77) & (cb <= 127) & (cr >= 133) & (cr <= 173)
        return float(skin.mean()) > 0.15
    except Exception:
        return False

def generate_passport_crop(original_path, bbox, delegate_id, conf_key=None):
    """Generate a passport-size (OUTPUT_WIDTH x OUTPUT_HEIGHT) image.
    The crop includes head, shoulders, and a top padding.
    Parameters:
        original_path: path to the original image file
        bbox: (x, y, w, h) face bounding box
        delegate_id: used for output filename
        conf_key: when given, output goes to storage/<conf_key>/preview/
    Returns:
        output_path: path to the generated preview image
    """
    img = Image.open(original_path)
    # Ensure RGB for consistent JPEG output and for pasting with white background
    if img.mode != 'RGB':
        img = img.convert('RGB')
    img_width, img_height = img.size
    x, y, w, h = bbox
    # Compute face center (x only) and face top
    face_cx = x + w / 2

    # Desired output size
    out_w = Config.OUTPUT_WIDTH
    out_h = Config.OUTPUT_HEIGHT

    # Determine target face height ratio in output (FACE_TARGET_RATIO)
    # We'll set the face height in the output image to FACE_TARGET_RATIO * out_h
    target_face_h = Config.FACE_TARGET_RATIO * out_h
    scale = target_face_h / h

    # Compute crop dimensions in original image
    crop_h = out_h / scale
    crop_w = out_w / scale

    # Apply top padding ratio relative to output height
    # top_padding is distance from crop top to FACE TOP (y) in original scale.
    # BUGFIX: previously used face_cy - top_padding which shifted crop down by h/2,
    # causing head/hair top to be cropped. Correct is y - top_padding.
    top_padding = Config.TOP_PADDING_RATIO * out_h / scale

    # --- Head margin ---
    # Haar bbox is tight around face and does NOT include hair/top-of-head.
    # Real head extends ~15-25% of face height above y. We reserve that inside
    # the top_padding so hair is not cut. With TOP_PADDING_RATIO=0.20 (94px)
    # and hair ~0.20*h*scale = ~42px, hair still has ~52px margin to top.
    # If original image has insufficient pixels above face, we pad instead of
    # clamping (see padding logic below) so head is never cropped.
    HEAD_MARGIN_RATIO = 0.22  # fraction of face height that is hair above bbox
    head_margin = HEAD_MARGIN_RATIO * h
    # Ensure crop top is above the estimated head top, not just face top
    # Effective crop top = y - top_padding ; head top = y - head_margin
    # So distance from crop top to head top = top_padding - head_margin
    # If head_margin > top_padding, hair would still be cut. In that case we
    # increase top_padding artificially to keep hair visible.
    if head_margin > top_padding:
        # Expand crop_h to include hair (keep bottom same, extend top)
        extra = head_margin - top_padding
        crop_y = y - head_margin - (top_padding - head_margin) * 0.3  # keep small 5% headroom above hair
        # Simpler: just set crop_y to head_top - small_headroom
        small_headroom = 0.05 * out_h / scale
        crop_y = (y - head_margin) - small_headroom
        # Need to re-derive crop_h if we changed top logic: keep crop_h same but
        # y-shift means bottom may exceed image; padding will handle it.
        # Recompute top_padding effective to include hair
        top_padding = head_margin + small_headroom
    else:
        crop_y = y - top_padding

    crop_x = face_cx - crop_w / 2

    # --- Decapitation guard ---
    # The crop window discards rows [0, crop_top). The crown/forehead lives in
    # the bbox x-range above the bbox top, so examine exactly that "crown
    # zone": rows [0, min(crop_top, bbox_top)), center half of the bbox width
    # (sides hold long hair / background, never the crown). If the zone holds
    # forehead skin, the crop would slice the crown (delegate 71 Balveer:
    # 1.86x zoom from an underestimated bbox cut crown AND chin) -> fall back
    # to the full-height cover window centered on the face: maximum head
    # preservation, never worse than the input photo.
    try:
        _ztop = max(0, int(round(crop_y)))
        _zbot = min(img_height, int(y))
        _zx0 = max(0, int(x + w / 4))
        _zx1 = min(img_width, int(x + 3 * w / 4))
        _decap = False
        if min(_ztop, _zbot) >= int(0.02 * img_height) and _zx1 > _zx0:
            _decap = _band_has_head(img.crop((_zx0, 0, _zx1, min(_ztop, _zbot))), h, img_height)
    except Exception:
        _decap = False
        if _by0 >= int(0.02 * img_height) and _bx1 > _bx0:
            _decap = _band_has_head(img.crop((_bx0, 0, _bx1, _by0)))
    except Exception:
        _decap = False
    if _decap:
        crop_h = float(img_height)
        crop_w = crop_h * out_w / out_h
        if crop_w > img_width:
            crop_w = float(img_width)
            crop_h = crop_w * out_h / out_w
        crop_x = face_cx - crop_w / 2
        crop_y = 0.0

    # --- Cover-fit crop (no white padding) ---
    # Previous white-canvas padding caused visible white bars for large faces
    # (e.g., Vivek Jain 412, Deepti 436, Sandeep 440) where desired crop
    # 479x639 > 354x472. User requires image to always fill 354x472 without white.
    # So we clamp crop to stay inside image. If desired crop is larger than
    # image, we scale to ensure crop fits (no white), accepting slightly larger
    # face than FACE_TARGET_RATIO but filling frame.
    # This also fixes head-top cropping while keeping cover behaviour.
    # Check if crop larger than image -> adjust scale to fit (zoom-in fallback)
    if crop_w > img_width or crop_h > img_height:
        # Need to zoom in so crop fits inside image
        scale_fit_w = out_w / img_width
        scale_fit_h = out_h / img_height
        scale_fit = max(scale_fit_w, scale_fit_h)
        # If current scale is smaller (would need zoom-out with white), increase to scale_fit
        if scale < scale_fit:
            scale = scale_fit
            crop_w = out_w / scale
            crop_h = out_h / scale
            top_padding = Config.TOP_PADDING_RATIO * out_h / scale
            head_margin = HEAD_MARGIN_RATIO * h
            if head_margin > top_padding:
                small_headroom = 0.05 * out_h / scale
                crop_y = (y - head_margin) - small_headroom
            else:
                crop_y = y - top_padding
            crop_x = face_cx - crop_w / 2

    # Clamp to stay inside image (no white)
    if crop_w > img_width:
        crop_w = img_width
        crop_x = 0
    else:
        if crop_x < 0:
            crop_x = 0
        elif crop_x + crop_w > img_width:
            crop_x = img_width - crop_w

    if crop_h > img_height:
        crop_h = img_height
        crop_y = 0
    else:
        if crop_y < 0:
            crop_y = 0
        elif crop_y + crop_h > img_height:
            crop_y = img_height - crop_h

    crop_x_int = int(round(crop_x))
    crop_y_int = int(round(crop_y))
    crop_w_int = int(round(crop_w))
    crop_h_int = int(round(crop_h))
    # Ensure ints stay in bounds after rounding
    crop_w_int = min(crop_w_int, img_width - crop_x_int)
    crop_h_int = min(crop_h_int, img_height - crop_y_int)

    crop_box = (crop_x_int, crop_y_int, crop_x_int + crop_w_int, crop_y_int + crop_h_int)
    cropped = img.crop(crop_box)
    resized = cropped.resize((out_w, out_h), Image.LANCZOS)

    output_path = os.path.join(_resolve_dirs(conf_key)[0], f"{delegate_id}.jpg")
    resized.save(output_path, format='JPEG', quality=95)
    return output_path
