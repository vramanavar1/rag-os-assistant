"""Generate the binary members of samples/corpus (docx, xlsx, pdf) and a sidecar example.

    uv run python scripts/make_sample_corpus.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "samples" / "corpus"


def pdf(pages: list[list[str]], title: str) -> bytes:
    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(len(pages)))}] "
            f"/Count {len(pages)} >>").encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    for i, lines in enumerate(pages):
        ops = ["BT", "/F1 11 Tf", "14 TL", "60 740 Td"]
        ops += [f"({ln.replace(chr(92), chr(92) * 2).replace('(', r'\(').replace(')', r'\)')}) Tj T*" for ln in lines]
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objs[4 + 2 * i] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                           f"/Contents {5 + 2 * i} 0 R >>").encode()
        objs[5 + 2 * i] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    info_id = 4 + 2 * len(pages)
    objs[info_id] = f"<< /Title ({title}) >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for n in sorted(objs):
        offsets[n] = len(out)
        out += f"{n} 0 obj\n".encode() + objs[n] + b"\nendobj\n"
    xref = len(out)
    size = max(objs) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{offsets[n]:010d} 00000 n \n".encode() for n in range(1, size))
    out += f"trailer\n<< /Size {size} /Root 1 0 R /Info {info_id} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def main() -> None:
    from docx import Document
    from openpyxl import Workbook

    # Travel & expense policy (PDF, 2 pages)
    (ROOT / "finance/global/policies/travel-expense-policy.pdf").write_bytes(pdf([
        ["Travel and Expense Policy", "Effective 1 February 2026. Applies to all employees globally.", "",
         "Booking: flights over 6 hours may be booked in premium economy.",
         "Hotels: the nightly cap is 180 EUR in EMEA, 220 USD in the Americas, 150 USD in APAC.",
         "Meals: a daily allowance of 60 EUR / 70 USD covers all meals while travelling."],
        ["Claims", "Expense claims must be submitted within 30 days with itemised receipts.",
         "Claims above 1,000 EUR require approval by the cost-center owner.",
         "Corporate cards must not be used for personal expenses."],
    ], "Travel and Expense Policy"))

    # Rate card (XLSX)
    wb = Workbook()
    ws = wb.active
    ws.title = "Rate card 2026"
    ws.append(["SKU", "Product", "Plan", "List price (USD/user/month)", "Volume discount >500 users"])
    for row in [("CC-BUS", "Contoso Connect", "Business", 12.5, "10%"),
                ("CC-ENT", "Contoso Connect", "Enterprise", 21.0, "15%"),
                ("CC-ADV", "Advanced Security add-on", "Add-on", 4.0, "5%"),
                ("CC-ARC", "Archive storage (per TB)", "Add-on", 30.0, "none")]:
        ws.append(list(row))
    wb.save(ROOT / "sales/us/pricing/rate-card-2026.xlsx")

    # MSA (DOCX) + sidecar sharing it explicitly with one HR employee (employee_id grant)
    d = Document()
    d.add_heading("Master Services Agreement - Fabrikam GmbH", level=1)
    d.add_paragraph("This Master Services Agreement is entered into by Contoso Ltd and Fabrikam GmbH.")
    d.add_heading("Term", level=2)
    d.add_paragraph("The initial term is 36 months, renewing automatically for 12-month periods unless either "
                    "party gives 90 days' written notice.")
    d.add_heading("Fees", level=2)
    t = d.add_table(rows=1, cols=3)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text, t.rows[0].cells[2].text = "Service", "Annual fee (EUR)", "Payment"
    for svc, fee, pay in [("Contoso Connect Enterprise (800 users)", "171,360", "Annually in advance"),
                          ("Premium support", "24,000", "Quarterly")]:
        r = t.add_row().cells
        r[0].text, r[1].text, r[2].text = svc, fee, pay
    d.add_heading("Liability", level=2)
    d.add_paragraph("Aggregate liability is capped at the fees paid in the preceding 12 months.")
    d.save(ROOT / "sales/emea/contracts/msa-fabrikam.docx")
    (ROOT / "sales/emea/contracts/msa-fabrikam.docx.meta.json").write_text(json.dumps(
        {"facets": {"doc_type": ["Contract"], "confidentiality": ["Confidential"]},
         "acl": {"employee_id": ["E1001"]}}, indent=2))
    print("sample corpus binaries written under", ROOT)


if __name__ == "__main__":
    main()
