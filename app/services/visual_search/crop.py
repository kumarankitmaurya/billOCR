"""Where cropping to the garment will go.

A full-frame photo — hanger, shop shelf, floor — dilutes the embedding with
background that has nothing to do with the design. Cropping to the garment
sharpens matching noticeably, which is why this seam exists in v1 even
though it does nothing yet.

When it is implemented (a lightweight segmenter, or a "tap the saree" crop
in the UI), it goes here and applies to both paths, because index-time and
query-time images must be treated identically or their vectors won't be
comparable. Note that image_store keeps the *uncropped* original, so the
catalog can be re-embedded with a real crop without re-photographing
anything.
"""

from PIL import Image


def crop_to_garment(image: Image.Image) -> Image.Image:
    """Return the image unchanged (v1 stub). See the module docstring."""
    return image
