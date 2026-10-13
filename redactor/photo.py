"""Photos and screenshots of documents, made into PDFs the scan path reads.

A phone photo of a statement is a scanned page with worse manners: taken at an
angle, on a desk, under a lamp, often sideways in the file and carrying the
phone's GPS position in its EXIF block. A screenshot of a payment app is a
page in dark mode. Neither needs a path of its own - the scanned-PDF path
already OCRs with two engines, boxes or rewrites what it finds, and blacks out
handwriting. Each needs only to arrive there looking like a scan:

* upright, by the orientation the camera recorded;
* cropped to the page and squared up, when a page can be told from what it
  lies on - which also takes the desk, a hand or the next sheet out of the
  picture before anything can be read off them;
* evenly lit, so a shadow is not mistaken for ink the OCR could not read;
* dark text on a light ground, so a dark-mode screen is not one block of
  "unread ink";
* rebuilt from its pixels alone into a fresh PDF, which leaves the EXIF block
  - GPS, the phone's make and serial, the time it was taken - behind.

Every frame of a multi-page TIFF becomes a page. The original file is never
changed.
"""

from __future__ import annotations

import io
from pathlib import Path

# Everything Pillow (with pillow-heif) opens that a document is ever saved as.
SUFFIXES = frozenset({
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".heic", ".heif", ".hif", ".avif",
    ".tif", ".tiff", ".webp", ".gif", ".bmp", ".dib", ".jp2", ".j2k", ".jpf", ".jpx",
})
# Formats whose frames are pages. An animated GIF or WEBP is one picture
# moving; a multi-frame TIFF is a fax or a scanner's whole document.
_PAGED = {"TIFF"}
# Lossless sources stay lossless: a screenshot's small text blurs under JPEG.
_LOSSLESS = {"PNG", "GIF", "BMP", "DIB", "TIFF"}

MAX_SIDE = 3300            # pixels on the long side: a letter page at 300 dpi
PAGE_LONG_SIDE = 792.0     # points: every page is 11 inches on its long side
WORK_SIDE = 1000           # pixels the page finder looks at

# The page finder takes a shape as a sheet of paper only when it covers a
# real share of the frame without being the frame, and is clearly brighter
# than what surrounds it. A light-mode screenshot's grey margin around a
# white card is not a desk, and cropping to the card would drop the header.
PAGE_MIN_AREA = 0.20
PAGE_MAX_AREA = 0.97
PAGE_MIN_CONTRAST = 40     # grey levels between the page and its surround
# What is cut away must be the surround the photo was taken against - the
# desk reaches the frame's edges, so it matches the edges' own colour. A dark
# or coloured header band inside a screenshot or on a statement does not, and
# cropping it off took the account holder's name with it.
SURROUND_MATCH = 0.85      # share of the cut-away that must look like the edges
SURROUND_DISTANCE = 60.0   # RGB distance still counted as the same surround
EDGE_TOUCH = 0.01          # a shape this near three frame edges fills the frame
# Text more than this far off level is turned upright when no page edge can
# be found to square it up by: OCR misses small slanted lines.
MAX_SKEW = 12.0            # degrees searched either way

# Uneven light is flattened when the page's own brightness, blurred far past
# any letter, still spans this many grey levels.
UNEVEN_LIGHT = 28
# ...and no part of the page is brightened by more than this. A shadow takes
# a page to about two thirds of its brightness; a coloured banner is far
# darker than that, and lifting it to white took its white lettering with it.
MAX_LIGHT_GAIN = 1.6
# A screen is dark mode when most of it is near black.
DARK_PIXEL = 80
DARK_SHARE = 0.55


def is_photo(path: Path | str) -> bool:
    return Path(path).suffix.lower() in SUFFIXES


def _register_heif() -> None:
    try:
        import pillow_heif
    except ImportError:                     # pragma: no cover - optional dependency
        return
    pillow_heif.register_heif_opener()


def open_pages(path: Path | str) -> list:
    """Each page of the image, upright and in RGB."""
    from PIL import Image, ImageOps, ImageSequence

    _register_heif()
    with Image.open(path) as image:
        fmt = (image.format or "").upper()
        # each frame copied as it is reached: the iterator hands back the one
        # image object seeked to the next frame, so collecting first gave a
        # twelve-page fax as twelve copies of page twelve
        frames = ImageSequence.Iterator(image) if fmt in _PAGED else [image]
        return [_rgb(ImageOps.exif_transpose(frame.copy())) for frame in frames]


def _rgb(image):
    """RGB on white: a transparent screenshot must not turn black."""
    from PIL import Image

    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        ground = Image.new("RGB", rgba.size, "white")
        ground.paste(rgba, mask=rgba.getchannel("A"))
        return ground
    if image.mode in ("I;16", "I;16B", "I;16L", "I"):
        return image.point(lambda v: v / 256).convert("RGB")
    return image.convert("RGB")


def prepare(image):
    """Upright page, cropped, evenly lit, dark text on light - see the module."""
    import numpy as np
    from PIL import Image

    if max(image.size) > MAX_SIDE:
        image = image.copy()
        image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    pixels = np.asarray(image)
    # squared up by the page's edges where they can be found, then levelled by
    # its text: a page run off the frame has no clean four corners, and what
    # slant the crop leaves is still enough to make OCR skip small lines
    pixels = _level(_crop_to_page(pixels))
    if _dark_mode(pixels):
        pixels = 255 - pixels
    pixels = _even_light(pixels)
    return Image.fromarray(pixels)


def _gray(pixels):
    import cv2
    return cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)


def _crop_to_page(pixels):
    """The page squared up, or the picture as it was if no page stands out."""
    import cv2
    import numpy as np

    height, width = pixels.shape[:2]
    scale = WORK_SIDE / max(height, width)
    size = (max(1, int(width * scale)), max(1, int(height * scale)))
    small = cv2.resize(_gray(pixels), size, interpolation=cv2.INTER_AREA)
    blurred = cv2.GaussianBlur(small, (7, 7), 0)
    # The page is whatever does not look like the frame's own edges - the desk
    # the photo was taken against reaches them. Not "whatever is bright": a
    # statement's dark banner is page, and brightness alone cut it off.
    colour = cv2.GaussianBlur(cv2.resize(pixels, size, interpolation=cv2.INTER_AREA)
                              .astype("float32"), (7, 7), 0)
    distance = np.linalg.norm(colour - _edge_colour(colour), axis=2)
    mask = (distance > SURROUND_DISTANCE).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return pixels
    page = max(contours, key=cv2.contourArea)
    share = cv2.contourArea(page) / float(small.shape[0] * small.shape[1])
    if not PAGE_MIN_AREA <= share <= PAGE_MAX_AREA:
        return pixels
    inside = np.zeros_like(small)
    cv2.drawContours(inside, [page], -1, 255, -1)
    if (float(np.median(blurred[inside > 0])) - float(np.median(blurred[inside == 0]))
            < PAGE_MIN_CONTRAST):
        return pixels
    if _fills_the_frame(page, small.shape) or not _cut_away_is_surround(
            pixels, inside, small.shape):
        return pixels
    corners = _four_corners(page)
    if corners is None:
        return pixels
    corners = corners / scale
    return _warp(pixels, corners)


def _level(pixels):
    """Turn slanted text level when no page edge could be found.

    The angle whose rows of ink are sharpest - text lines and the gaps between
    them line up only when level - is found on a small grey copy and applied
    to the full picture.
    """
    import cv2
    import numpy as np

    gray = _gray(pixels)
    scale = WORK_SIDE / max(gray.shape)
    small = cv2.resize(gray, (max(1, int(gray.shape[1] * scale)), max(1, int(gray.shape[0] * scale))),
                       interpolation=cv2.INTER_AREA)
    ink = cv2.adaptiveThreshold(small, 1, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15)
    if ink.mean() < 0.002:            # a blank page has no slant to measure
        return pixels
    centre = (small.shape[1] / 2, small.shape[0] / 2)

    def sharpness(angle: float) -> float:
        turned = cv2.warpAffine(ink, cv2.getRotationMatrix2D(centre, angle, 1.0),
                                (small.shape[1], small.shape[0]))
        return float(np.var(turned.sum(axis=1)))

    angles = np.arange(-MAX_SKEW, MAX_SKEW + 0.01, 0.5)
    best = max(angles, key=sharpness)
    if abs(best) < 0.75 or sharpness(best) < 1.05 * sharpness(0.0):
        return pixels
    height, width = pixels.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), float(best), 1.0)
    return cv2.warpAffine(pixels, matrix, (width, height), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def _fills_the_frame(contour, shape) -> bool:
    """A shape reaching three edges of the frame is the screen, not a sheet on it.

    Two is not enough: a hand-held shot routinely runs the page off its right
    and bottom edges, and refusing those left a statement skewed on the desk,
    its slanted transaction lines unread and unredacted.
    """
    import cv2

    x, y, w, h = cv2.boundingRect(contour)
    rows, cols = shape[:2]
    margin_x, margin_y = cols * EDGE_TOUCH, rows * EDGE_TOUCH
    touches = sum((x <= margin_x, y <= margin_y,
                   x + w >= cols - margin_x, y + h >= rows - margin_y))
    return touches >= 3


def _edge_colour(colour):
    """The median colour of the frame's outermost pixels: what the page lies on."""
    import numpy as np

    edge = np.concatenate([colour[:3].reshape(-1, 3), colour[-3:].reshape(-1, 3),
                           colour[:, :3].reshape(-1, 3), colour[:, -3:].reshape(-1, 3)])
    return np.median(edge, axis=0)


def _cut_away_is_surround(pixels, inside, shape) -> bool:
    """Whether what lies outside the page looks like the frame's own edges."""
    import cv2
    import numpy as np

    rows, cols = shape[:2]
    colour = cv2.resize(pixels, (cols, rows), interpolation=cv2.INTER_AREA).astype("float32")
    surround = _edge_colour(colour)
    outside = colour[inside == 0]
    if not len(outside):
        return False
    near = np.linalg.norm(outside - surround, axis=1) <= SURROUND_DISTANCE
    return float(near.mean()) >= SURROUND_MATCH


def _four_corners(contour):
    """The sheet's four corners: from the outline if it simplifies to four,
    else the tightest rotated rectangle around it (a dog-eared corner)."""
    import cv2
    import numpy as np

    perimeter = cv2.arcLength(contour, True)
    for epsilon in (0.02, 0.03, 0.04, 0.05):
        approx = cv2.approxPolyDP(contour, epsilon * perimeter, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            return approx.reshape(4, 2).astype("float32")
    return cv2.boxPoints(cv2.minAreaRect(contour)).astype("float32")


def _warp(pixels, corners):
    import cv2
    import numpy as np

    # top-left, top-right, bottom-right, bottom-left
    total = corners.sum(axis=1)
    diff = np.diff(corners, axis=1).ravel()
    ordered = np.array([corners[np.argmin(total)], corners[np.argmin(diff)],
                        corners[np.argmax(total)], corners[np.argmax(diff)]], dtype="float32")
    tl, tr, br, bl = ordered
    width = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    height = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    if width < 50 or height < 50:
        return pixels
    target = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
                      dtype="float32")
    matrix = cv2.getPerspectiveTransform(ordered, target)
    return cv2.warpPerspective(pixels, matrix, (width, height), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def _dark_mode(pixels) -> bool:
    import numpy as np
    return float(np.mean(_gray(pixels) < DARK_PIXEL)) >= DARK_SHARE


def _even_light(pixels):
    """Divide out the lighting: a shadow is not ink."""
    import cv2
    import numpy as np

    gray = _gray(pixels)
    side = max(15, (min(gray.shape) // 12) | 1)
    # the page without its print: close away the letters, then blur
    ground = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, np.ones((side // 3 | 1,) * 2, np.uint8))
    ground = cv2.GaussianBlur(ground, (side, side), 0)
    low, high = np.percentile(ground, (5, 95))
    if high - low < UNEVEN_LIGHT:
        return pixels
    ground = np.maximum(ground.astype("float32"), 1.0)[..., None]
    gain = np.minimum(np.float32(255.0) / ground, np.float32(MAX_LIGHT_GAIN))
    flattened = pixels.astype("float32") * gain
    return np.clip(flattened, 0, 255).astype("uint8")


def to_pdf(source: Path | str, target: Path | str) -> Path:
    """The photo as a new PDF, one page per image page, nothing else carried."""
    import pymupdf
    from PIL import Image

    source, target = Path(source), Path(target)
    _register_heif()
    with Image.open(source) as probe:
        lossless = (probe.format or "").upper() in _LOSSLESS
    pages = open_pages(source)
    doc = pymupdf.open()
    try:
        for page_image in pages:
            image = prepare(page_image)
            buffer = io.BytesIO()
            if lossless:
                image.save(buffer, format="PNG", optimize=True)
            else:
                image.save(buffer, format="JPEG", quality=90)
            ratio = PAGE_LONG_SIDE / max(image.size)
            width, height = image.size[0] * ratio, image.size[1] * ratio
            page = doc.new_page(width=width, height=height)
            page.insert_image(page.rect, stream=buffer.getvalue())
        doc.set_metadata({})
        target.parent.mkdir(parents=True, exist_ok=True)
        doc.save(target, garbage=3, deflate=True)
    finally:
        doc.close()
    return target
