from __future__ import annotations

import hashlib
import html
import io
import re
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
QUOTE_SPLIT_RE = re.compile(
    r"(?im)^\s*(?:[-_]{2,}\s*)?(?:"
    r"original message|forwarded message|исходное сообщение|пересланное сообщение|"
    r"from:|от:|отправлено:|кому:|тема:|sent:|to:|subject:"
    r")\b.*$"
)
QUOTE_MARKER_RE = re.compile(
    r"(?im)^\s*(?:[-_]{2,}\s*)?(?:"
    r"original message|forwarded message|исходное сообщение|пересланное сообщение|"
    r"from:|от:|отправлено:|кому:|тема:|sent:|to:|subject:"
    r")\b"
)


def clean_ws(text: str | None, limit: int | None = None) -> str:
    text = WS_RE.sub(" ", text or "").strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    # ── Расклейка таблиц: ячейки → разделитель, строки → перенос. ──
    # Без этого <td>Картридж</td><td>111 228</td> слипались в "Картридж111 228".
    text = re.sub(r"(?i)</t[dh]>\s*<t[dh][^>]*>", " | ", text)     # граница ячеек (| переживает clean_ws)
    text = re.sub(r"(?i)</tr>", "\n", text)                         # конец строки таблицы
    text = re.sub(r"(?i)<t[dh][^>]*>", " ", text)                   # открывающая ячейка
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    # Схлопываем пробелы/табы, но СОХРАНЯЕМ переносы строк (структуру таблицы)
    lines = []
    for line in text.split("\n"):
        cleaned = re.sub(r"[^\S\n]+", " ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def visible_body(body_text: str | None, body_html: str | None) -> str:
    # Important: run quote splitting before whitespace normalization.
    # If newlines are collapsed first, line-anchored markers like "From:" / "От:" stop working.
    # Если в HTML есть таблица — берём расклеенный HTML (plain-text часть слипшаяся),
    # иначе обычный приоритет body_text.
    if body_html and re.search(r"(?i)<table", body_html):
        text = html_to_text(body_html)
    else:
        text = body_text or html_to_text(body_html)
    parts = QUOTE_SPLIT_RE.split(text, maxsplit=1)
    return clean_ws(parts[0] if parts else text)


def select_visible_text(
    body_text: str | None,
    body_html: str | None,
    visible_text: str | None = None,
) -> str:
    """Единый детерминированный выбор видимого текста для импорта/reprocess/классификации.

    Приоритет:
    1) уже расклеенный visible_text (содержит " | " — колонки таблицы/XLSX) — берём как есть,
       не тратим время на повторную расклейку 25КБ HTML;
    2) если в HTML есть <table> — расклеиваем из HTML;
    3) иначе — visible_text как есть или расклейка из body_text/HTML.
    """
    vt = str(visible_text or "")
    if " | " in vt:
        return vt[:60000]
    bh = body_html or ""
    # Кап размера HTML ПЕРЕД расклейкой: regex по мегабайтам HTML вешает процесс
    # (катастрофический backtracking). Товарная строка/реквизиты всегда в начале письма.
    if len(bh) > 200000:
        bh = bh[:200000]
    bt = body_text or ""
    if len(bt) > 200000:
        bt = bt[:200000]
    if bh and "<table" in bh.lower():
        return visible_body(bt, bh)[:60000]
    return (vt or visible_body(bt, bh))[:60000]


def quote_marker_count(body_text: str | None, body_html: str | None) -> int:
    text = body_text or html_to_text(body_html)
    return len(QUOTE_MARKER_RE.findall(text or ""))


def _header_value(msg: Any, name: str) -> str | None:
    value = msg.get(name)
    return str(value) if value is not None else None


def _addresses(value: str | None) -> str | None:
    if not value:
        return None
    pairs = getaddresses([value])
    items = []
    for display, addr in pairs:
        if display and addr:
            items.append(f"{display} <{addr}>")
        elif addr:
            items.append(addr)
    return ", ".join(items) or value


def _received_at(msg: Any) -> str | None:
    value = msg.get("Date")
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        return None


def _references(value: str | None) -> list[str]:
    if not value:
        return []
    return re.findall(r"<[^>]+>", value)


def _normalize_message_id(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"<[^>]+>", value)
    return m.group(0).strip() if m else value.strip()


def _extract_xls_text(data: bytes) -> str:
    """Старый бинарный формат .xls — openpyxl его НЕ читает, нужен xlrd."""
    try:
        import xlrd
        book = xlrd.open_workbook(file_contents=data)
        lines: list[str] = []
        for sheet in book.sheets():
            for r in range(min(sheet.nrows, 200)):
                cells = []
                for c in sheet.row_values(r):
                    if isinstance(c, float) and c.is_integer():
                        c = int(c)  # 79633.0 → 79633 (номера документов)
                    cells.append(str(c).strip() if c not in (None, "") else "")
                if any(cells):
                    lines.append(" | ".join(cells))
        return "\n".join(lines)
    except Exception:
        return ""


def _extract_xlsx_text(data: bytes) -> str:
    """Extract readable text from an Excel file for pattern matching and AI."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        lines: list[str] = []
        for sheet in wb.worksheets:
            rows_read = 0
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c).strip() if c is not None else "" for c in row]
                non_empty = [c for c in cells if c]
                if non_empty:
                    # Разделитель " | " переживает clean_ws (в отличие от \t),
                    # поэтому колонки XLSX остаются различимы для паттернов.
                    lines.append(" | ".join(cells))
                    rows_read += 1
                if rows_read >= 200:
                    break
        text = "\n".join(lines)
        if text.strip():
            return text
    except Exception:
        pass
    # openpyxl не справился (старый .xls или битый zip) → пробуем xlrd.
    return _extract_xls_text(data)


def _extract_pdf_text(data: bytes) -> str:
    """Best-effort text extraction from PDF (requires pdfminer or pypdf if installed)."""
    try:
        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams
        out = io.StringIO()
        extract_text_to_fp(io.BytesIO(data), out, laparams=LAParams(), output_type="text", codec="utf-8")
        return out.getvalue()[:8000]
    except Exception:
        pass
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages[:20]:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)[:8000]
    except Exception:
        return ""


def parse_email_bytes(raw: bytes, mailbox: str, uid: str, raw_path: str | None = None) -> dict[str, Any]:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    body_text_parts: list[str] = []
    body_html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []

    parts = msg.walk() if msg.is_multipart() else [msg]

    for part in parts:
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        payload_bytes = None
        try:
            payload_bytes = part.get_payload(decode=True)
        except Exception:
            payload_bytes = None

        if filename or disposition == "attachment":
            att_bytes = payload_bytes or b""
            att_size = len(att_bytes)
            extracted_text = ""
            lower_fname = (filename or "").lower()
            # Only extract text from small attachments (< 3 MB) to keep import fast.
            # Large files are processed later by AI/Vision on demand.
            MAX_EXTRACT_BYTES = 3 * 1024 * 1024
            if att_size < MAX_EXTRACT_BYTES:
                if lower_fname.endswith((".xlsx", ".xls")) or "spreadsheet" in ctype or "excel" in ctype:
                    extracted_text = _extract_xlsx_text(att_bytes)
                elif lower_fname.endswith(".pdf") or "pdf" in ctype:
                    extracted_text = _extract_pdf_text(att_bytes)
            attachments.append(
                {
                    "filename": filename,
                    "content_type": ctype,
                    "size_bytes": att_size,
                    "_bytes": att_bytes,
                    "extracted_text": extracted_text,
                }
            )
            continue

        try:
            content = part.get_content()
        except Exception:
            try:
                content = (payload_bytes or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                content = ""

        if ctype == "text/plain":
            body_text_parts.append(str(content))
        elif ctype == "text/html":
            body_html_parts.append(str(content))

    body_text_raw = "\n".join(body_text_parts)
    body_html = "\n".join(body_html_parts)
    visible = visible_body(body_text_raw, body_html)
    body_text = clean_ws(body_text_raw)

    # Append extracted attachment content so patterns and AI can use it
    attachment_texts: list[str] = []
    for att in attachments:
        extracted = att.pop("extracted_text", "") or ""
        if extracted.strip():
            attachment_texts.append(f"[Вложение: {att.get('filename', '')}]\n{extracted.strip()}")
    if attachment_texts:
        extra = "\n\n" + "\n\n".join(attachment_texts)
        body_text = (body_text + extra).strip()
        visible = (visible + extra).strip()
    message_id = _normalize_message_id(_header_value(msg, "Message-ID"))

    return {
        "mailbox": mailbox,
        "uid": str(uid),
        "message_id": message_id,
        "in_reply_to": _normalize_message_id(_header_value(msg, "In-Reply-To")),
        "references": _references(_header_value(msg, "References")),
        "subject": _header_value(msg, "Subject"),
        "from_addr": _addresses(_header_value(msg, "From")),
        "to_addr": _addresses(_header_value(msg, "To")),
        "cc_addr": _addresses(_header_value(msg, "Cc")),
        "received_at": _received_at(msg),
        "body_text": body_text,
        "body_html": body_html,
        "visible_text": visible,
        "snippet": clean_ws(visible or body_text or html_to_text(body_html), 400),
        "quote_markers": quote_marker_count(body_text_raw, body_html),
        "raw_hash": hashlib.sha256(raw).hexdigest(),
        "raw_path": raw_path,
        "attachments": attachments,
    }


def parse_eml_file(path: Path, mailbox: str = "eml_inbox") -> dict[str, Any]:
    raw = path.read_bytes()
    uid = hashlib.sha1(str(path.resolve()).encode("utf-8") + b":" + raw[:128]).hexdigest()[:24]
    return parse_email_bytes(raw, mailbox=mailbox, uid=uid, raw_path=str(path))
