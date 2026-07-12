import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

import httpx

from core import character_loader, session_manager
from server import app


class ApiSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.test_root = Path(self.tempdir.name) / "projects"
        self.old_session_root = session_manager.ROOT_DIR
        self.old_character_root = character_loader.ROOT_DIR
        self.old_locks = session_manager._save_locks
        self.old_guard = session_manager._locks_guard
        session_manager.ROOT_DIR = self.test_root
        character_loader.ROOT_DIR = self.test_root
        session_manager._save_locks = {}
        session_manager._locks_guard = asyncio.Lock()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        session_manager.ROOT_DIR = self.old_session_root
        character_loader.ROOT_DIR = self.old_character_root
        session_manager._save_locks = self.old_locks
        session_manager._locks_guard = self.old_guard
        self.tempdir.cleanup()

    async def test_path_and_snapshot_contract_returns_400(self):
        cases = [
            ("/api/session", {"project": "../secret", "save": "默认存档"}),
            ("/api/session", {"project": "默认项目", "save": r"..\secret"}),
            ("/api/session", {"project": r"C:\Windows", "save": "默认存档"}),
            ("/api/session/snapshot", {
                "project": "默认项目", "save": "默认存档",
                "filename": "其他存档.20260712_121212.json",
            }),
            ("/api/session/snapshot", {
                "project": "默认项目", "save": "默认存档",
                "filename": "../默认存档.20260712_121212.json",
            }),
        ]
        for url, params in cases:
            with self.subTest(url=url, params=params):
                response = await self.client.get(url, params=params)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(list(Path(self.tempdir.name).rglob("*")), [])

    async def test_invalid_import_returns_400_and_writes_nothing(self):
        fixture = {
            "session_id": "恶意导入",
            "message_history": [{"role": "user", "content": "hello"}],
            "characters_state": {
                "角色A": {"name": "角色A", "affinity": "<img onerror=alert(1)>"},
            },
        }
        response = await self.client.post(
            "/api/sessions/import",
            json={
                "project": "测试项目",
                "name": "恶意导入",
                "json_str": json.dumps(fixture, ensure_ascii=False),
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(list(Path(self.tempdir.name).rglob("*.json")), [])

    async def test_chinese_project_name_and_normalization_collision(self):
        created = await self.client.post("/api/projects", json={"name": "中文 世界"})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["name"], "中文_世界")
        self.assertTrue((self.test_root / "中文_世界" / "saves" / "默认存档.json").exists())

        collision = await self.client.post("/api/projects", json={"name": "中文　世界"})
        self.assertEqual(collision.status_code, 409, collision.text)
        self.assertEqual(len(list(self.test_root.iterdir())), 1)

    async def test_character_worldbook_and_project_ids_use_same_policy(self):
        character = await self.client.put(
            "/api/characters/CON",
            params={"project": "默认项目"},
            json={"data": {"id": "CON"}},
        )
        self.assertEqual(character.status_code, 400, character.text)

        fullwidth_id = quote("Ａ", safe="")
        worldbook = await self.client.put(
            f"/api/worldbook/{fullwidth_id}",
            params={"project": "默认项目"},
            json={"data": {"id": "Ａ"}},
        )
        self.assertEqual(worldbook.status_code, 400, worldbook.text)

        project = await self.client.post("/api/projects", json={"name": r"..\outside"})
        self.assertEqual(project.status_code, 400, project.text)
        self.assertEqual(list(Path(self.tempdir.name).rglob("*.yaml")), [])

    async def test_snapshot_payload_owner_mismatch_returns_400(self):
        history = self.test_root / "默认项目" / "saves" / ".history"
        history.mkdir(parents=True)
        snapshot = history / "默认存档.20260712_121212.json"
        snapshot.write_text(json.dumps({
            "session_id": "其他存档",
            "project": "默认项目",
            "message_history": [],
        }, ensure_ascii=False), encoding="utf-8")
        response = await self.client.get("/api/session/snapshot", params={
            "project": "默认项目",
            "save": "默认存档",
            "filename": snapshot.name,
        })
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("不属于当前存档", response.text)

    async def test_security_headers_are_present(self):
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        csp = response.headers.get("content-security-policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(response.headers.get("referrer-policy"), "no-referrer")


if __name__ == "__main__":
    unittest.main()
