import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo

class SubtitleQCFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_review_lock_delivery_and_overwrite_protection(self):
        cue = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal 海豹在冰面", "expected_revision": 0})
        self.assertEqual(cue["version_revision"], 1)
        comment = self.db.add_comment(self.version, "carol", {"cue_id": cue["id"], "time_ms": 1200, "body": "术语正确，请确认冻结时间"}, "reviewer")
        self.assertEqual(comment["time_ms"], 1200)
        self.db.submit(self.version, "bob")
        approved = self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.assertEqual(approved["status"], "approved")
        self.db.lock(self.version, "alice")
        delivery = self.db.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 1})

    def test_revision_overlap_glossary_and_permissions(self):
        first = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.db.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 2500, "end_ms": 4000, "text": "另一句", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "禁用译法"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "密封装置", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "权限"):
            self.db.save_cue(self.version, "carol", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "海豹", "expected_revision": 1})


class CueRevisionHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def _revisions(self):
        return self.db.list_revisions(self.version)

    def test_every_save_appends_old_to_new_revision(self):
        cue = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹在冰面", "expected_revision": 0})
        first = self._revisions()
        self.assertEqual(len(first), 1)
        self.assertIsNone(first[0]["old_text"])
        self.assertEqual(first[0]["new_text"], "海豹在冰面")
        self.assertEqual(first[0]["revision_seq"], 1)
        self.assertEqual(first[0]["changed_by"], "bob")
        self.assertTrue(first[0]["created_at"])
        self.assertEqual(first[0]["version_revision"], 1)

        self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 3200, "text": "海豹在冰面上休息", "expected_revision": 1})
        revs = self._revisions()
        self.assertEqual(len(revs), 2)
        self.assertEqual(revs[1]["old_text"], "海豹在冰面")
        self.assertEqual(revs[1]["new_text"], "海豹在冰面上休息")
        self.assertEqual(revs[1]["old_end_ms"], 3000)
        self.assertEqual(revs[1]["new_end_ms"], 3200)
        self.assertEqual(revs[1]["revision_seq"], 2)

    def test_rejection_stacks_new_revisions_without_losing_history(self):
        cue = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "第一版翻译", "expected_revision": 0})
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "reject", "comment": "请重译"}, "reviewer")
        self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "第二版翻译", "expected_revision": 1})
        self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "第三版翻译", "expected_revision": 2})
        texts = [(r["old_text"], r["new_text"]) for r in self._revisions()]
        self.assertEqual(texts, [
            (None, "第一版翻译"),
            ("第一版翻译", "第二版翻译"),
            ("第二版翻译", "第三版翻译"),
        ])

    def test_revisions_freeze_after_lock_and_delivery(self):
        cue = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹在冰面", "expected_revision": 0})
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.db.lock(self.version, "alice")
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "锁定后修改", "expected_revision": 1})
        self.assertEqual(len(self._revisions()), 1)

        with self.db.connect() as conn:
            import sqlite3
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE cue_revisions SET new_text='篡改' WHERE cue_id=?", (cue["id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM cue_revisions WHERE cue_id=?", (cue["id"],))
            conn.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO cue_revisions(cue_id,version_id,revision_seq,new_text,new_start_ms,new_end_ms,new_cue_index,changed_by,version_revision,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cue["id"], self.version, 2, "补记", 1000, 3000, 1, "mallory", 1, "2026-01-01T00:00:00+00:00"),
                )
            conn.rollback()

        self.db.deliver(self.version, "alice")
        self.assertEqual(len(self._revisions()), 1)
        self.assertEqual(self._revisions()[0]["new_text"], "海豹在冰面")

    def test_revisions_grouped_per_cue(self):
        first = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "字幕甲", "expected_revision": 0})
        self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 4000, "end_ms": 5000, "text": "字幕乙", "expected_revision": 1})
        self.db.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "字幕甲修改", "expected_revision": 2})
        revs = self._revisions()
        self.assertEqual([r["cue_id"] for r in revs], [first["id"], first["id"] + 1, first["id"]])
        self.assertEqual([r["revision_seq"] for r in revs if r["cue_id"] == first["id"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
