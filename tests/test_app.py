import sqlite3
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
        # 所有失败的保存都不能留下修订记录
        self.assertEqual([r["revision_no"] for r in self.db.list_cue_revisions(self.version)], [1])

    def test_revisions_record_old_new_and_stack_after_reject(self):
        cue = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal 海豹在冰面", "expected_revision": 0})
        self.assertEqual(cue["revision_no"], 1)
        self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2800, "text": "seal 海豹趴在冰面", "expected_revision": 1})
        self.db.submit(self.version, "bob")
        rejected = self.db.review(self.version, "carol", {"decision": "reject", "comment": "时码和措辞都要改"}, "reviewer")
        self.assertEqual(rejected["status"], "draft")
        self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2600, "text": "seal 海豹趴在浮冰面", "expected_revision": 2})
        revisions = self.db.list_cue_revisions(self.version, cue["id"])
        self.assertEqual([r["revision_no"] for r in revisions], [1, 2, 3])
        self.assertEqual(revisions[0]["change_type"], "create")
        self.assertIsNone(revisions[0]["old_text"])
        self.assertEqual(revisions[0]["new_text"], "seal 海豹在冰面")
        self.assertEqual(revisions[1]["change_type"], "update")
        self.assertEqual(revisions[1]["old_text"], "seal 海豹在冰面")
        self.assertEqual(revisions[1]["new_text"], "seal 海豹趴在冰面")
        self.assertEqual((revisions[1]["old_end_ms"], revisions[1]["new_end_ms"]), (3000, 2800))
        self.assertEqual(revisions[2]["old_text"], "seal 海豹趴在冰面")
        self.assertEqual(revisions[2]["new_text"], "seal 海豹趴在浮冰面")
        self.assertTrue(all(r["changed_by"] == "bob" and r["created_at"] for r in revisions))
        self.assertEqual(self.db.list_cue_revisions(self.version), revisions)

    def test_revision_records_are_immutable(self):
        self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal 海豹在冰面", "expected_revision": 0})
        with self.db.connect() as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "不可修改"):
                conn.execute("UPDATE cue_revisions SET new_text='篡改'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "不可删除"):
                conn.execute("DELETE FROM cue_revisions")
        self.assertEqual(len(self.db.list_cue_revisions(self.version)), 1)

    def test_revisions_frozen_after_lock_and_delivery(self):
        cue = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal 海豹在冰面", "expected_revision": 0})
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve", "comment": ""}, "reviewer")
        self.db.lock(self.version, "alice")
        self.db.deliver(self.version, "alice")
        before = self.db.list_cue_revisions(self.version)
        self.assertEqual(len(before), 1)
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 1})
        self.assertEqual(self.db.list_cue_revisions(self.version), before)
        with self.db.connect() as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "不能再补记"):
                conn.execute(
                    "INSERT INTO cue_revisions(version_id,cue_id,cue_index,revision_no,version_revision,change_type,new_text,new_start_ms,new_end_ms,changed_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (self.version, cue["id"], 1, 99, 1, "update", "补记", 0, 1, "mallory", "2026-01-01T00:00:00+00:00"),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "不可修改"):
                conn.execute("UPDATE cue_revisions SET new_text='篡改' WHERE id=?", (before[0]["id"],))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "不可删除"):
                conn.execute("DELETE FROM cue_revisions WHERE id=?", (before[0]["id"],))
        self.assertEqual(self.db.list_cue_revisions(self.version), before)


if __name__ == "__main__":
    unittest.main()
