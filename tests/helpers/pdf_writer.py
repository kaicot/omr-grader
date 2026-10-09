"""Write small PDFs for tests without a PDF library.

Each page shows one lossless image stretched over the page, the way a scanner's PDF does.
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

# Points; A4 landscape, the size the synthetic answer sheets are drawn for.
LANDSCAPE_A4 = (842.0, 595.0)

type PageImage = bytes | NDArray[np.uint8] | None


def _rgb(image: bytes | NDArray[np.uint8]) -> NDArray[np.uint8]:
    if isinstance(image, bytes):
        decoded = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if decoded is None:
            raise ValueError("page image could not be decoded")
        image = decoded
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    return np.ascontiguousarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))


def pdf_bytes(
    pages: Sequence[PageImage],
    *,
    page_size: tuple[float, float] = LANDSCAPE_A4,
    rotation: int = 0,
    encrypted: bool = False,
) -> bytes:
    """A PDF with one page per entry; ``None`` makes a blank page.

    ``encrypted`` adds a standard security handler whose user password is not empty, so a
    reader must refuse to open the file without a password.
    """
    width, height = page_size
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")
    page_tree = add(b"")
    kids: list[int] = []
    for image in pages:
        resources = b"<< >>"
        content = b""
        if image is not None:
            rgb = _rgb(image)
            data = zlib.compress(rgb.tobytes())
            xobject = add(
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB"
                b" /BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n"
                % (rgb.shape[1], rgb.shape[0], len(data))
                + data
                + b"\nendstream"
            )
            resources = b"<< /XObject << /Im0 %d 0 R >> >>" % xobject
            content = b"q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (width, height)
        contents = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        kids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Rotate %d"
                b" /Resources %s /Contents %d 0 R >>"
                % (page_tree, width, height, rotation, resources, contents)
            )
        )
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % page_tree
    objects[page_tree - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % kid for kid in kids),
        len(kids),
    )
    encrypt = 0
    if encrypted:
        # RC4 128-bit handler with /O and /U that no empty password matches.
        encrypt = add(
            b"<< /Filter /Standard /V 2 /R 3 /Length 128 /P -3904"
            b" /O <" + b"11" * 32 + b"> /U <" + b"22" * 32 + b"> >>"
        )

    output = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(output)
    output += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    output += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    trailer = b"/Size %d /Root %d 0 R" % (len(objects) + 1, catalog)
    if encrypt:
        trailer += b" /Encrypt %d 0 R /ID [<%s> <%s>]" % (encrypt, b"ab" * 16, b"ab" * 16)
    output += b"trailer\n<< " + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(output)


def write_pdf(path: Path, pages: Sequence[PageImage], **options: object) -> Path:
    path.write_bytes(pdf_bytes(pages, **options))  # type: ignore[arg-type]
    return path
