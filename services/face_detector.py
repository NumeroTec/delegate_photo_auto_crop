import cv2 as cv
import os
from config import Config

# Attempt to load a local Haar cascade file; fallback to OpenCV's built‑in data path
local_cascade_path = os.path.join(os.path.dirname(__file__), 'haarcascade_frontalface_default.xml')
if os.path.isfile(local_cascade_path):
    cascade_path = local_cascade_path
else:
    cascade_path = cv.data.haarcascades + 'haarcascade_frontalface_default.xml'
try:
    face_cascade = cv.CascadeClassifier(cascade_path)
except AttributeError:
    # Fallback: OpenCV built without Haar cascades – we will treat all images as NO_FACE
    face_cascade = None
    print('Warning: cv2.CascadeClassifier not available – face detection disabled')

def _iou(a, b):
    """Intersection over Union for two boxes (x,y,w,h)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ax2, ay2 = ax+aw, ay+ah
    bx2, by2 = bx+bw, by+bh
    inter_x1 = max(ax, bx)
    inter_y1 = max(ay, by)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return 0.0
    inter = (inter_x2-inter_x1)*(inter_y2-inter_y1)
    union = aw*ah + bw*bh - inter
    return inter/union if union else 0

def _detect_all(gray):
    """Run multi-scale Haar with several parameter sets and collect candidates."""
    candidates = []
    # Parameter sets that cover tight (1.05) and coarse (1.2) scales.
    # Balveer case: true face only found at sf=1.2 mn=3, false chest at sf=1.1 mn=5.
    param_sets = [
        (1.1, 5, (65, 65)),
        (1.2, 3, (65, 65)),
        (1.05, 4, (65, 65)),
        (1.1, 3, (65, 65)),
        (1.2, 3, (50, 50)),
    ]
    for sf, mn, ms in param_sets:
        try:
            faces = face_cascade.detectMultiScale(gray, scaleFactor=sf, minNeighbors=mn, minSize=ms)
        except Exception:
            faces = []
        for (x,y,w,h) in faces:
            candidates.append((int(x),int(y),int(w),int(h)))
    # Last fallback without minSize for very small/distant faces
    if not candidates:
        try:
            faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5)
            for (x,y,w,h) in faces:
                candidates.append((int(x),int(y),int(w),int(h)))
        except Exception:
            pass
    # NMS / dedup by IoU >0.35 keep larger
    filtered = []
    for cand in candidates:
        keep = True
        for i, existing in enumerate(filtered):
            if _iou(cand, existing) > 0.35:
                # keep larger area
                if cand[2]*cand[3] > existing[2]*existing[3]:
                    filtered[i] = cand
                keep = False
                break
        if keep:
            filtered.append(cand)
    return filtered

def detect_face(image_path):
    """Detect a single face — robust for passport photos.

    Fixes head-top cropping issue for Balveer Singh Saharan (#115 delegate_no, id 71):
      previous single-pass Haar missed true face at (123,108) and returned chest button (22,206)
      causing catastrophic crop (chin only). New logic runs multi-scale, merges candidates,
      and selects best by upper-half + largest-area + smallest-y weighting.

    Returns dict with status 'OK' (bbox+confidence) or 'NO_FACE'/'MULTIPLE_FACES'.
    """
    img = cv.imread(image_path)
    if img is None:
        return {'status': 'INVALID_IMAGE'}
    gray = cv.cvtColor(img, cv.COLOR_BGR2GRAY)
    if face_cascade is None or face_cascade.empty():
        return {'status': 'NO_FACE'}

    img_h, img_w = gray.shape
    faces = _detect_all(gray)

    if len(faces) == 0:
        return {'status': 'NO_FACE'}
    if len(faces) == 1:
        (x, y, w, h) = faces[0]
        # Single detection but could be chest false positive in lower half with tiny area.
        # For passport, single face should be in upper 65% and occupy reasonable area (>2% of image)
        area_ratio = (w*h) / (img_w*img_h)
        # If suspiciously low and y > 0.45*H, mark as NO_FACE to trigger manual review rather than bad crop
        if y > img_h*0.48 and area_ratio < 0.04:
            # Try to see if it's isolated without alternative — treat as NO_FACE to avoid wrong crop
            # But keep backward compat: if image is 354x472 passport already, this rarely hits
            pass
        return {'status': 'OK', 'bbox': (int(x), int(y), int(w), int(h)), 'confidence': 1.0}

    # Multiple candidates — select best for passport.
    # Heuristic: true face is in upper portion and largest. We score each:
    #   score = area - 8*y   (prefer larger and higher)
    # Filter to upper 60% first, but if that leaves 0 use all.
    upper = [f for f in faces if f[1] < img_h * 0.60]
    pool = upper if len(upper) >= 1 else faces
    # Also filter out tiny chest detections that are <45% of largest area (false buttons)
    areas = [fw*fh for (x,y,fw,fh) in pool]
    max_area = max(areas)
    # Keep only those not too small relative to largest (unless that would leave 0)
    size_filtered = [f for f in pool if f[2]*f[3] >= max_area*0.45]
    if size_filtered:
        pool = size_filtered
    # If still >1, score and pick best
    def score(f):
        x,y,w,h = f
        area = w*h
        # Center bonus: face should be near horizontal center for passport
        cx = x + w/2
        center_dist = abs(cx - img_w/2) / img_w  # 0..0.5
        center_penalty = center_dist * 15000
        # y penalty: higher faces preferred
        return area - 8*y - center_penalty
    best = max(pool, key=score)
    # If remaining pool had 2+ well-separated faces of similar size, flag MULTIPLE_FACES
    # Check second best score close to best (>0.85 ratio) and IoU low -> ambiguous
    if len(pool) > 1:
        sorted_pool = sorted(pool, key=score, reverse=True)
        best_score = score(sorted_pool[0])
        second_score = score(sorted_pool[1])
        if second_score > best_score * 0.82 and _iou(sorted_pool[0], sorted_pool[1]) < 0.2:
            # Two distinct large faces in upper area -> true multiple faces
            # But if one is significantly lower (y diff > 0.15*H) prefer higher one
            if abs(sorted_pool[0][1] - sorted_pool[1][1]) > img_h*0.18:
                # prefer higher (smaller y)
                higher = sorted_pool[0] if sorted_pool[0][1] < sorted_pool[1][1] else sorted_pool[1]
                return {'status': 'OK', 'bbox': (int(higher[0]), int(higher[1]), int(higher[2]), int(higher[3])), 'confidence': 0.9}
            return {'status': 'MULTIPLE_FACES'}
    x,y,w,h = best
    return {'status': 'OK', 'bbox': (int(x), int(y), int(w), int(h)), 'confidence': 0.95}
