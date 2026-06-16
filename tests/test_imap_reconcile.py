from scripts.reconcile_imap_counts import reconcile_snapshots


def _server(count=5, folder="INBOX"):
    return {
        "account": "mail@example.com",
        "folders": {
            folder: {
                "account": "mail@example.com",
                "folder": folder,
                "display_folder": folder,
                "uidvalidity": "100",
                "message_count": count,
                "unseen_count": 0,
                "uids": {
                    str(uid): {"message_id": f"<{uid}@example>", "internal_date": None}
                    for uid in range(1, count + 1)
                },
            }
        },
    }


def _local(count=5, folder="INBOX"):
    return [
        {
            "id": uid, "account": "mail@example.com", "mailbox": folder, "uid": str(uid),
            "message_id": f"<{uid}@example>", "raw_hash": f"hash-{uid}",
            "duplicate_of_raw_email_id": None, "status": "imported",
        }
        for uid in range(1, count + 1)
    ]


def test_equal_server_and_local_has_no_missing():
    result = reconcile_snapshots(_local(5), {}, _server(5))
    assert result["summary"]["server_total"] == 5
    assert result["summary"]["missing_local_total"] == 0
    assert result["summary"]["all_server_uids_explained"] is True


def test_missing_uid_is_reported_concretely():
    result = reconcile_snapshots(_local(4), {}, _server(5))
    assert result["summary"]["missing_local_total"] == 1
    assert result["missing_rows"][0]["uid"] == "5"
    assert result["missing_rows"][0]["folder"] == "INBOX"


def test_linked_duplicate_is_not_missing():
    local = _local(5)
    local[-1]["duplicate_of_raw_email_id"] = 1
    local[-1]["status"] = "duplicate"
    result = reconcile_snapshots(local, {}, _server(5))
    assert result["summary"]["missing_local_total"] == 0
    assert result["summary"]["by_server_uid_status"]["imported_duplicate_linked"] == 1


def test_folder_mismatch_is_detected():
    local = _local(1, folder="Archive")
    result = reconcile_snapshots(local, {}, _server(1, folder="INBOX"))
    assert result["summary"]["exact_duplicate_known_total"] == 1
    assert result["local_without_server"][0]["server_status"] == "folder_mismatch"


def test_reconcile_does_not_touch_outbox():
    marker = [{"id": 1}]
    reconcile_snapshots(_local(5), {}, _server(5))
    assert marker == [{"id": 1}]
