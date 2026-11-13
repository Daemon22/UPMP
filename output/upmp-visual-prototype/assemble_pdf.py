from __future__ import annotations

from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
SOURCE = PROJECT / "output" / "pdf" / "UPMP-process-assurance-river-map-concept.pdf"
TEMP = PROJECT / "tmp" / "pdfs" / "UPMP-process-assurance-river-map-concept.updated.pdf"
SCREENS = [
    ROOT / "previews" / "overview.png",
    ROOT / "previews" / "verification-detail.png",
    ROOT / "previews" / "button-lineage.png",
    ROOT / "previews" / "complex-overview.png",
    ROOT / "previews" / "architecture.png",
    ROOT / "previews" / "mutation-detail.png",
]


def overlay_page(width: float, height: float, page_number: int, total: int, light: bool, cover: bool = False):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(width, height), pageCompression=1)
    if cover:
        c.setFillColor(HexColor("#142C43"))
        c.rect(width - 62, 6, 58, 16, fill=1, stroke=0)
        c.rect(width - 100, 32, 96, 17, fill=1, stroke=0)
        c.setFillColor(HexColor("#D9E7F0"))
        c.setFont("Helvetica-Bold", 6.4)
        c.drawRightString(width - 11, 37, f"{page_number} / {total}")
        c.save()
        buffer.seek(0)
        return PdfReader(buffer).pages[0]
    if light:
        c.setFillColor(HexColor("#F4F7FA"))
        c.rect(width - 62, 6, 58, 16, fill=1, stroke=0)
        c.setFillColor(HexColor("#375673"))
    else:
        c.setFillColor(HexColor("#081A2B"))
        c.rect(width - 62, 6, 58, 16, fill=1, stroke=0)
        c.setFillColor(HexColor("#D9E7F0"))
    c.setFont("Helvetica-Bold", 6.4)
    c.drawRightString(width - 11, 10, f"{page_number} / {total}")
    c.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def make_screen_page(path: Path):
    image = ImageReader(str(path))
    width, height = 960, 540
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(width, height), pageCompression=1)
    c.drawImage(image, 0, 0, width=width, height=height, preserveAspectRatio=False, mask="auto")
    c.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def main():
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    for path in SCREENS:
        if not path.exists():
            raise FileNotFoundError(path)

    source = PdfReader(str(SOURCE))
    if len(source.pages) == 7:
        original_content = [source.pages[i] for i in range(5)] + [source.pages[6]]
    elif len(source.pages) == 9:
        original_content = [source.pages[i] for i in range(5)] + [source.pages[8]]
    elif len(source.pages) == 12:
        original_content = [source.pages[i] for i in range(5)] + [source.pages[11]]
    else:
        raise ValueError(f"Expected the original seven-page or updated nine-page concept PDF, found {len(source.pages)} pages")

    pages = original_content[:5]
    pages.extend(make_screen_page(path) for path in SCREENS)
    pages.append(original_content[5])
    total = len(pages)

    writer = PdfWriter()
    for index, page in enumerate(pages, start=1):
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        page.merge_page(overlay_page(width, height, index, total, index in {2, 3, 4, 5, 12}, cover=index == 1))
        writer.add_page(page)

    metadata = dict(source.metadata or {})
    metadata.update({
        "/Title": "UPMP Process Assurance and Interface Concept",
        "/Subject": "Layered process architecture, orchestration, evidence fabric, actual progress, deviations, mutations, goal guards, and output lineage",
    })
    writer.add_metadata(metadata)
    TEMP.parent.mkdir(parents=True, exist_ok=True)
    with TEMP.open("wb") as stream:
        writer.write(stream)
    TEMP.replace(SOURCE)
    print(f"Updated {SOURCE} with {total} pages; added the complex ecosystem, architecture, and mutation deep-dive views.")


if __name__ == "__main__":
    main()
