"""Грабер окна без VPN: ловит 200, по КАЖДОЙ avtoto-странице — сохраняет HTML, парсит
поля, СКАЧИВАЕТ фото и ПРИВЯЗЫВАЕТ их к кейсу (attachments), чтобы оператор видел брак.

Запуск: docker exec -e PYTHONPATH=/app readmail_21 python3 /app/app/_avtoto_grab.py [секунды]
Пока крутится — выключи VPN; на первом 200 хватает всё разом, пишет отчёт.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time

from app.runtime_settings import apply_runtime_settings
apply_runtime_settings()

from app.link_fetcher import classify_link, _fetch_url, parse_avtoto_claim

DB = "/app/data/readmail.sqlite3"
ATT_DIR = "/app/data/attachments"
HTML_DIR = "/app/data/avtoto_html"
RESULT = "/app/data/avtoto_grab_result.txt"
URL_RE = re.compile(r'https?://[^\s"<>)\]\\]+')


def _log(msg: str) -> None:
    print(msg, flush=True)
    try:
        with open(RESULT, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _claim_urls_by_email() -> list[tuple[int, str]]:
    """[(raw_email_id, claim_url)] — каждая avtoto-страница со своим письмом."""
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    out: list[tuple[int, str]] = []
    seen: set[str] = set()
    for r in db.execute("SELECT id, body_text, body_html FROM raw_emails ORDER BY id DESC").fetchall():
        t = " ".join(str(r[k] or "") for k in ("body_text", "body_html"))
        for u in URL_RE.findall(t):
            u = u.rstrip(".,;)]}\"'")
            if classify_link(u) in ("avtoto_claim", "avtoto_nondelivery") and u not in seen:
                seen.add(u)
                out.append((int(r["id"]), u))
    db.close()
    return out


def _attach_photo(raw_email_id: int, url: str, idx: int, content: bytes, ct: str) -> bool:
    """Сохранить фото на диск и привязать к письму (attachments). Дедуп по имени."""
    ext = ".jpg"
    low = url.lower()
    for e in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if low.endswith(e):
            ext = e
            break
    if "png" in (ct or ""):
        ext = ".png"
    fname = f"avtoto_{raw_email_id}_{idx}{ext}"
    case_dir = os.path.join(ATT_DIR, str(raw_email_id))
    os.makedirs(case_dir, exist_ok=True)
    fpath = os.path.join(case_dir, fname)
    db = sqlite3.connect(DB)
    try:
        ex = db.execute("SELECT 1 FROM attachments WHERE raw_email_id=? AND filename=?", (raw_email_id, fname)).fetchone()
        if ex:
            db.close()
            return False
        with open(fpath, "wb") as f:
            f.write(content)
        db.execute(
            "INSERT INTO attachments(raw_email_id, filename, content_type, size_bytes, file_path) VALUES (?,?,?,?,?)",
            (raw_email_id, fname, ct or "image/jpeg", len(content), fpath),
        )
        db.commit()
        return True
    finally:
        db.close()


def main() -> None:
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    open(RESULT, "w").close()
    os.makedirs(HTML_DIR, exist_ok=True)
    pairs = _claim_urls_by_email()
    if not pairs:
        _log("Нет avtoto-ссылок в корпусе.")
        return
    _log(f"=== ГРАБЕР avtoto: {duration}с, страниц к захвату: {len(pairs)}. ВЫКЛЮЧАЙ VPN! ===")
    test_url = pairs[0][1]
    t0 = time.time()
    attempt = 0
    while time.time() - t0 < duration:
        attempt += 1
        raw, ct, st = _fetch_url(test_url, timeout=10)
        el = int(time.time() - t0)
        if st == 200 and raw:
            _log(f"[{el:02d}s] ✅ Окно открылось! Хватаю {len(pairs)} страниц…")
            break
        _log(f"[{el:02d}s] попытка {attempt}: HTTP {st} (ждём выключения VPN…)")
        time.sleep(3)
    else:
        _log("=== Окно не открылось за отведённое время. VPN не выключен? ===")
        return

    pages_ok = photos_total = 0
    for i, (rid, url) in enumerate(pairs):
        r2, c2, s2 = _fetch_url(url, timeout=12)
        if s2 != 200 or not r2:
            _log(f"  • письмо {rid}: HTTP {s2} — пропуск")
            continue
        html = r2.decode("utf-8", errors="replace")
        try:
            with open(os.path.join(HTML_DIR, f"email_{rid}.html"), "w", encoding="utf-8") as hf:
                hf.write(html)
        except Exception:
            pass
        parsed = parse_avtoto_claim(html)
        fields = {k: v for k, v in (parsed.get("fields") or {}).items() if v}
        photos = parsed.get("photos") or []
        att_n = 0
        for j, purl in enumerate(photos):
            pr, pc, ps = _fetch_url(purl, timeout=12)
            if ps == 200 and pr and len(pr) > 1500:  # >1.5КБ = не заглушка
                if _attach_photo(rid, purl, j, pr, pc or "image/jpeg"):
                    att_n += 1
        pages_ok += 1
        photos_total += att_n
        _log(f"  • письмо {rid}: поля={fields} | фото на стр={len(photos)} | привязано={att_n}")
    _log(f"=== ГОТОВО: страниц {pages_ok}/{len(pairs)}, фото привязано {photos_total}. ВКЛЮЧАЙ VPN! ===")


if __name__ == "__main__":
    main()
