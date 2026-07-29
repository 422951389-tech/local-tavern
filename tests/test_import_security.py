import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from core import session_manager
from core.import_validation import MAX_IMPORT_BYTES, validate_import_json


def valid_session() -> dict:
    return {
        "session_id": "导入测试",
        "name": "导入测试",
        "message_history": [
            {"role": "user", "content": "<script>alert('literal')</script>"},
        ],
        "characters_state": {
            "角色A": {"name": "角色A", "affinity": 10},
        },
    }


class ImportValidationTests(unittest.TestCase):
    def test_invalid_top_level_and_oversized_text_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "顶层必须是对象"):
            validate_import_json("[]")
        with self.assertRaisesRegex(ValueError, "不能超过 8 MiB"):
            validate_import_json(" " * (MAX_IMPORT_BYTES + 1))

    def test_wrong_role_and_malicious_affinity_type_are_rejected(self):
        fixture = valid_session()
        fixture["message_history"][0]["role"] = "system"
        with self.assertRaisesRegex(ValueError, "message_history"):
            validate_import_json(json.dumps(fixture, ensure_ascii=False))

        fixture = valid_session()
        fixture["characters_state"]["角色A"]["affinity"] = "<img src=x onerror=alert(1)>"
        with self.assertRaisesRegex(ValueError, "affinity"):
            validate_import_json(json.dumps(fixture, ensure_ascii=False))

    def test_revision_and_message_uuid_are_strictly_validated(self):
        fixture = valid_session()
        fixture["revision"] = -1
        with self.assertRaisesRegex(ValueError, "revision"):
            validate_import_json(json.dumps(fixture, ensure_ascii=False))

        fixture = valid_session()
        fixture["revision"] = "2"
        with self.assertRaisesRegex(ValueError, "revision"):
            validate_import_json(json.dumps(fixture, ensure_ascii=False))

        fixture = valid_session()
        fixture["message_history"][0]["id"] = "not-a-uuid"
        with self.assertRaisesRegex(ValueError, "消息 id 必须是 UUID"):
            validate_import_json(json.dumps(fixture, ensure_ascii=False))

    def test_message_presentation_is_bounded_and_round_trips_without_raw(self):
        fixture = valid_session()
        fixture["message_history"][0].update({
            "role": "assistant",
            "content": """📍 导入地点 | ⏱️ 午后 / 晴
🎯 [主线] 任务名称：验证刷新
📌 当前场景：结构化内容已导入。
➡️ 下一目标：继续验证
👤 用户：导入用户
🎭 角色A | 💝 100%
📖 场景旁白
结构化旁白
💡 行动建议
- 继续
""",
            "presentation": {
                "schema_version": 1,
                "scene_meta": {
                    "location": "导入地点",
                    "time_weather": "午后 / 晴",
                    "main_quest": "验证刷新",
                    "current_scene": "结构化内容已导入。",
                    "next_goal": "继续验证",
                    "user_line": "导入用户",
                },
                "characters": [{
                    "name": "角色A",
                    "affinity": 60,
                    "previous_affinity": 50,
                    "mood": "平静",
                }],
                "narration": "结构化旁白",
                "suggestions": ["继续"],
                "warnings": [],
                "scene_changes": [{"key": "location", "value": "导入地点"}],
            },
        })
        validated = validate_import_json(json.dumps(fixture, ensure_ascii=False))
        presentation = validated["message_history"][0]["presentation"]
        self.assertEqual(presentation["scene_meta"]["main_quest"], "验证刷新")
        self.assertEqual(presentation["characters"][0]["affinity"], 60)
        self.assertEqual(presentation["characters"][0]["previous_affinity"], 50)
        self.assertEqual(presentation["characters"][0]["mood"], "平静")
        self.assertEqual(presentation["scene_changes"][0]["key"], "location")
        self.assertNotIn("raw", presentation)

        mismatched = json.loads(json.dumps(fixture, ensure_ascii=False))
        mismatched["message_history"][0]["presentation"]["scene_meta"][
            "main_quest"
        ] = "伪造主线"
        with self.assertRaisesRegex(ValueError, "与消息原文不一致"):
            validate_import_json(json.dumps(mismatched, ensure_ascii=False))

        fixture["message_history"][0]["presentation"]["schema_version"] = 2
        with self.assertRaisesRegex(ValueError, "presentation.schema_version"):
            validate_import_json(json.dumps(fixture, ensure_ascii=False))

        fixture["message_history"][0]["presentation"]["schema_version"] = 1
        fixture["message_history"][0]["presentation"]["suggestions"] = [
            f"建议{index}" for index in range(6)
        ]
        with self.assertRaisesRegex(ValueError, "presentation.suggestions"):
            validate_import_json(json.dumps(fixture, ensure_ascii=False))

        fixture["message_history"][0]["presentation"]["suggestions"] = ["继续"]
        fixture["message_history"][0]["presentation"]["raw"] = "不得重复导入原文"
        with self.assertRaisesRegex(ValueError, "presentation.raw"):
            validate_import_json(json.dumps(fixture, ensure_ascii=False))


class IsolatedImportWriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_root = session_manager.ROOT_DIR
        session_manager.ROOT_DIR = Path(self.tempdir.name) / "projects"
        (session_manager.ROOT_DIR / "测试项目").mkdir(parents=True, exist_ok=True)
        session_manager.clear_session_stores_for_testing()

    async def asyncTearDown(self):
        session_manager.ROOT_DIR = self.old_root
        session_manager.clear_session_stores_for_testing()
        self.tempdir.cleanup()

    async def test_valid_html_like_text_is_saved_as_literal_text(self):
        fixture = valid_session()
        fixture["revision"] = 99
        data = await session_manager.import_session(
            "测试项目",
            json.dumps(fixture, ensure_ascii=False),
            "导入 剧情",
        )
        target = session_manager.ROOT_DIR / "测试项目" / "saves" / "导入_剧情.json"
        self.assertTrue(target.exists())
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(saved["message_history"][0]["content"], "<script>alert('literal')</script>")
        self.assertEqual(data["session_id"], "导入_剧情")
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(data["revision"], 1)
        UUID(saved["message_history"][0]["id"])

    async def test_missing_display_name_falls_back_to_session_id(self):
        fixture = valid_session()
        del fixture["name"]
        data = await session_manager.import_session(
            "测试项目",
            json.dumps(fixture, ensure_ascii=False),
        )
        self.assertEqual(data["name"], "导入测试")
        self.assertTrue((session_manager.ROOT_DIR / "测试项目" / "saves" / "导入测试.json").exists())

    async def test_invalid_schema_creates_no_save_file(self):
        fixture = valid_session()
        fixture["characters_state"]["角色A"]["affinity"] = {"html": "<img onerror=alert(1)>"}
        with self.assertRaises(ValueError):
            await session_manager.import_session(
                "测试项目",
                json.dumps(fixture, ensure_ascii=False),
                "坏导入",
            )
        self.assertEqual(list(Path(self.tempdir.name).rglob("*.json")), [])

    async def test_path_syntax_and_normalization_collision_do_not_overwrite(self):
        fixture_text = json.dumps(valid_session(), ensure_ascii=False)
        with self.assertRaises(ValueError):
            await session_manager.import_session("测试项目", fixture_text, r"..\evil")
        self.assertEqual(list(Path(self.tempdir.name).rglob("*.json")), [])

        await session_manager.import_session("测试项目", fixture_text, "Ａ")
        target = session_manager.ROOT_DIR / "测试项目" / "saves" / "A.json"
        before = target.read_bytes()
        with self.assertRaises(FileExistsError):
            await session_manager.import_session("测试项目", fixture_text, "A")
        self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
