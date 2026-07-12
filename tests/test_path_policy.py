import tempfile
import unittest
from pathlib import Path

from core.path_policy import (
    PathPolicyError,
    display_name_to_id,
    resolve_snapshot_path,
    resolve_under,
    validate_file_id,
    validate_snapshot_filename,
)


class PathPolicyTests(unittest.TestCase):
    def test_chinese_display_name_gets_stable_id(self):
        self.assertEqual(display_name_to_id("中文 存档"), "中文_存档")
        self.assertEqual(display_name_to_id("Ａ"), display_name_to_id("A"))
        self.assertEqual(validate_file_id("默认存档"), "默认存档")

    def test_invalid_ids_are_rejected(self):
        invalid = [
            "", ".", "..", "../secret", r"..\secret", r"C:\Windows",
            r"\\server\share", "/tmp/save", "CON", "aux.txt", " name",
            "name ", "with space", "name%2Fchild", "Ａ", ".hidden", "name.",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(PathPolicyError):
                    validate_file_id(value)

    def test_resolve_under_never_leaves_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = resolve_under(root, "project", "saves")
            self.assertTrue(child.is_relative_to(root.resolve()))
            with self.assertRaises(PathPolicyError):
                resolve_under(root, "..", "outside")

    def test_snapshot_filename_belongs_to_save_and_known_type(self):
        name, kind = validate_snapshot_filename(
            "默认存档",
            "默认存档.trim.20260712_121212.json",
        )
        self.assertEqual(name, "默认存档.trim.20260712_121212.json")
        self.assertEqual(kind, "trim")

        unique_name = "默认存档.20260712_121212_123456_deadbeef.json"
        name, kind = validate_snapshot_filename("默认存档", unique_name)
        self.assertEqual(name, unique_name)
        self.assertEqual(kind, "snapshot")

        invalid = [
            "其他存档.20260712_121212.json",
            "默认存档.unknown.20260712_121212.json",
            "默认存档.2026-07-12.json",
            "默认存档.20260712_121212_123456_NOTHEX00.json",
            "../默认存档.20260712_121212.json",
            r"C:\默认存档.20260712_121212.json",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(PathPolicyError):
                    validate_snapshot_filename("默认存档", value)

    def test_trim_snapshot_cannot_resolve_for_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PathPolicyError):
                resolve_snapshot_path(
                    Path(tmp),
                    "默认项目",
                    "默认存档",
                    "默认存档.trim.20260712_121212.json",
                    allowed_types=("snapshot", "reset"),
                )


if __name__ == "__main__":
    unittest.main()
