from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import struct
import zlib

from PIL import Image
from pypdf import PdfReader
import pytest

from openthesis.financial_sources.image_pdf import image_pdf_from_png


def _png(mode: str, size: tuple[int, int], color) -> bytes:
    stream = BytesIO()
    Image.new(mode, size, color).save(stream, format="PNG")
    return stream.getvalue()


def _header_only_png(width: int, height: int) -> bytes:
    """Declare dimensions but omit pixel data so validation must precede decode."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", b"") + chunk(b"IEND", b""))


def _image_xobject(pdf: bytes):
    reader = PdfReader(BytesIO(pdf))
    page = reader.pages[0]
    resources = page["/Resources"].get_object()
    xobjects = resources["/XObject"].get_object()
    return reader, page, xobjects["/Im0"].get_object()


def test_rgb_page_pdf_contains_exact_lossless_pixels_and_is_textless():
    pixels = bytes((12, 34, 56, 78, 90, 123, 210, 22, 43, 255, 254, 253))
    image = Image.frombytes("RGB", (2, 2), pixels)
    encoded = BytesIO()
    image.save(encoded, format="PNG")

    pdf = image_pdf_from_png(encoded.getvalue(), resolution=108)

    reader, page, xobject = _image_xobject(pdf)
    assert len(reader.pages) == 1
    assert page.extract_text() == ""
    assert xobject["/Width"] == 2
    assert xobject["/Height"] == 2
    assert xobject["/ColorSpace"] == "/DeviceRGB"
    assert xobject["/BitsPerComponent"] == 8
    assert xobject["/Filter"] == "/FlateDecode"
    assert xobject.get_data() == pixels
    assert b"/Im0 Do" in page.get_contents().get_data()
    assert abs(float(page.mediabox.width) - 2 * 72 / 108) < 1e-6
    assert abs(float(page.mediabox.height) - 2 * 72 / 108) < 1e-6


def test_rgba_pixels_are_composited_onto_white_before_embedding():
    image = Image.frombytes("RGBA", (3, 1), bytes((255, 0, 0, 128, 10, 20, 30, 255, 7, 8, 9, 0)))
    encoded_stream = BytesIO()
    image.save(encoded_stream, format="PNG")

    _reader, _page, xobject = _image_xobject(image_pdf_from_png(encoded_stream.getvalue()))

    assert xobject.get_data() == bytes((255, 127, 127, 10, 20, 30, 255, 255, 255))


def test_identical_png_and_resolution_produce_identical_pdf_bytes():
    encoded = _png("RGB", (4, 3), (20, 40, 60))

    first = image_pdf_from_png(encoded, resolution=144.0)
    second = image_pdf_from_png(encoded, resolution=144.0)

    assert sha256(first).digest() == sha256(second).digest()
    assert first == second


@pytest.mark.parametrize("resolution", [True, False, 0, -1, float("nan"), float("inf"), -float("inf"), "108", None])
def test_invalid_resolution_is_rejected_with_a_fixed_code(resolution):
    with pytest.raises(ValueError, match="^IMAGE_PDF_RESOLUTION_INVALID$"):
        image_pdf_from_png(_png("RGB", (1, 1), (0, 0, 0)), resolution=resolution)


def test_non_png_and_multiframe_png_are_rejected_with_fixed_codes():
    with pytest.raises(ValueError, match="^IMAGE_PDF_PNG_INVALID$"):
        image_pdf_from_png(b"not a png")

    jpeg = BytesIO()
    Image.new("RGB", (1, 1), (1, 2, 3)).save(jpeg, format="JPEG")
    with pytest.raises(ValueError, match="^IMAGE_PDF_PNG_INVALID$"):
        image_pdf_from_png(jpeg.getvalue())

    first = Image.new("RGB", (1, 1), (1, 2, 3))
    second = Image.new("RGB", (1, 1), (4, 5, 6))
    animated = BytesIO()
    first.save(animated, format="PNG", save_all=True, append_images=[second])
    with pytest.raises(ValueError, match="^IMAGE_PDF_MULTIFRAME_UNSUPPORTED$"):
        image_pdf_from_png(animated.getvalue())


def test_encoded_size_and_dimensions_are_bounded_before_pixel_load():
    with pytest.raises(ValueError, match="^IMAGE_PDF_INPUT_TOO_LARGE$"):
        image_pdf_from_png(b"x" * (8 * 1024 * 1024 + 1))

    too_wide = _header_only_png(4097, 1)
    with pytest.raises(ValueError, match="^IMAGE_PDF_DIMENSIONS_INVALID$"):
        image_pdf_from_png(too_wide)

    too_many_pixels = _header_only_png(4096, 3000)
    with pytest.raises(ValueError, match="^IMAGE_PDF_DIMENSIONS_INVALID$"):
        image_pdf_from_png(too_many_pixels)
