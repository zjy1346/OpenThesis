"""Bounded, lossless, in-memory conversion of one PNG page to a one-page PDF."""
from __future__ import annotations

from io import BytesIO
import math
import zlib

from PIL import Image, UnidentifiedImageError
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)


_MAX_INPUT_BYTES = 8 * 1024 * 1024
_MAX_SIDE = 4096
_MAX_PIXELS = 12_000_000


def _bad_png() -> ValueError:
    return ValueError("IMAGE_PDF_PNG_INVALID")


def image_pdf_from_png(png_bytes: bytes, *, resolution: float = 108.0) -> bytes:
    """Encode a bounded single-frame PNG as a lossless, one-page PDF in memory.

    Each image pixel maps to ``72 / resolution`` PDF points. Transparent pixels
    are composited onto white; all image samples are otherwise preserved as RGB.
    """
    if (isinstance(resolution, bool) or not isinstance(resolution, (int, float))):
        raise ValueError("IMAGE_PDF_RESOLUTION_INVALID")
    try:
        dpi = float(resolution)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("IMAGE_PDF_RESOLUTION_INVALID") from None
    if not math.isfinite(dpi) or dpi <= 0:
        raise ValueError("IMAGE_PDF_RESOLUTION_INVALID")

    if not isinstance(png_bytes, bytes):
        raise ValueError("IMAGE_PDF_INPUT_INVALID")
    if len(png_bytes) > _MAX_INPUT_BYTES:
        raise ValueError("IMAGE_PDF_INPUT_TOO_LARGE")
    if not png_bytes:
        raise _bad_png()

    try:
        source = Image.open(BytesIO(png_bytes))
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise _bad_png() from None
    if source.format != "PNG":
        source.close()
        raise _bad_png()

    try:
        width, height = source.size
        if (width <= 0 or height <= 0 or width > _MAX_SIDE or height > _MAX_SIDE
                or width * height > _MAX_PIXELS):
            raise ValueError("IMAGE_PDF_DIMENSIONS_INVALID")
        if source.n_frames != 1:
            raise ValueError("IMAGE_PDF_MULTIFRAME_UNSUPPORTED")
        # All resource bounds are checked against the header before decompression.
        source.load()
    except ValueError as exc:
        if str(exc) in {"IMAGE_PDF_DIMENSIONS_INVALID", "IMAGE_PDF_MULTIFRAME_UNSUPPORTED"}:
            source.close()
            raise
        source.close()
        raise _bad_png() from None
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        source.close()
        raise _bad_png() from None

    rgba = rgb_image = None
    try:
        has_transparency = "A" in source.getbands() or "transparency" in source.info
        if has_transparency:
            rgba = source.convert("RGBA")
            rgb_image = Image.new("RGB", (width, height), (255, 255, 255))
            rgb_image.paste(rgba, (0, 0), rgba.getchannel("A"))
        else:
            rgb_image = source.convert("RGB")
        rgb_bytes = rgb_image.tobytes()
    except (OSError, ValueError):
        raise _bad_png() from None
    finally:
        source.close()
        if rgba is not None:
            rgba.close()
        if rgb_image is not None:
            rgb_image.close()

    page_width = width * 72.0 / dpi
    page_height = height * 72.0 / dpi
    if (not math.isfinite(page_width) or not math.isfinite(page_height)
            or page_width <= 0 or page_height <= 0):
        raise ValueError("IMAGE_PDF_RESOLUTION_INVALID")

    writer = PdfWriter()
    page = writer.add_blank_page(width=page_width, height=page_height)

    image = DecodedStreamObject()
    image.set_data(zlib.compress(rgb_bytes))
    image.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(width),
        NameObject("/Height"): NumberObject(height),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(8),
        NameObject("/Filter"): NameObject("/FlateDecode"),
    })
    image_ref = writer._add_object(image)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): image_ref}),
    })

    content = DecodedStreamObject()
    content.set_data(
        f"q\n{page_width} 0 0 {page_height} 0 0 cm\n/Im0 Do\nQ\n".encode("ascii")
    )
    page[NameObject("/Contents")] = writer._add_object(content)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()
