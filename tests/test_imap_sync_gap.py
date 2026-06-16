from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.audit_imap_sync_gap import build_report, load_local, open_db_ro


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE raw_emails (id INTEGER PRIMARY KEY, mailbox TEXT, uid TEXT, "
        "message_id TEXT, canonical_key TEXT)"
    )
    con.execute(
        "CREATE TABLE import_uid_failures (mailbox TEXT, uid TEXT, stage TEXT, "
        "error_type TEXT, error_message TEXT, attempts INTEGER, status TEXT)"
    )
    con.executemany(
        "INSERT INTO raw_emails(mailbox, uid, message_id, canonical_key) VALUES (?,?,?,?)",
        [
            ("FolderA", "1", "<a1>", "message_id:<a1>"),
            ("FolderA", "2", "<a2>", "message_id:<a2>"),
            ("FolderB", "10", "<b1>", "message_id:<b1>"),
        ],
    )
    # uid 5 на сервере отсутствует локально, но это известный сбой (объяснимо)
    con.execute(
        "INSERT INTO import_uid_failures VALUES ('FolderA','5','fetch_single','abort','x',3,'quarantined')"
    )
    con.commit()
    con.close()


class AuditSyncGapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.sqlite3"
        _make_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_db_opens_readonly(self):
        con = open_db_ro(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("INSERT INTO raw_emails(mailbox, uid) VALUES ('x','9')")
        con.close()

    def test_dry_run_local_only(self):
        con = open_db_ro(self.db)
        local = load_local(con)
        con.close()
        report = build_report(local, server=None, db_path=self.db)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["totals"]["local"], 3)
        self.assertIsNone(report["totals"]["server"])

    def test_live_gap_classification(self):
        con = open_db_ro(self.db)
        local = load_local(con)
        con.close()
        # сервер: FolderA имеет 1,2,5,7 ; FolderB имеет 10
        server = {"FolderA": ["1", "2", "5", "7"], "FolderB": ["10"]}
        report = build_report(local, server=server, db_path=self.db)
        self.assertEqual(report["mode"], "live")
        self.assertEqual(report["totals"]["missing_on_local"], 2)  # uid 5 и 7
        self.assertEqual(report["totals"]["local_only"], 0)
        cats = {(d["uid"]): d["category"] for d in report["_decisions"]}
        # uid 5 объясним известным сбоем, uid 7 — настоящая дыра
        self.assertTrue(cats["5"].startswith("known_failure:quarantined"))
        self.assertEqual(cats["7"], "unexplained")


if __name__ == "__main__":
    unittest.main()
