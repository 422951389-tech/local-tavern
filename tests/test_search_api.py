import json
import tempfile
import tracemalloc
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import httpx

from core import character_loader, search_service, session_manager
from core.config import PORT
from server import app


MESSAGE_A = "11111111-1111-4111-8111-111111111111"
MESSAGE_B = "22222222-2222-4222-8222-222222222222"
MESSAGE_C = "33333333-3333-4333-8333-333333333333"
SUMMARY_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def message(
    message_id: str,
    content: str,
    *,
    role: str = "user",
    pinned: bool = False,
    time: str = "2026-07-18T12:00:00",
) -> dict:
    return {
        "id": message_id,
        "role": role,
        "content": content,
        "pinned": pinned,
        "in_prompt": True,
        "time": time,
    }


def summary(
    *,
    text: str = "",
    time: str = "",
    facts: list[str] | None = None,
    relations: list[str] | None = None,
) -> dict:
    return {
        "id": SUMMARY_A,
        "status": "completed",
        "text": text,
        "time": time,
        "facts": list(facts or []),
        "relations": list(relations or []),
        "created_at": "2026-07-18T12:30:00",
    }


def session_payload(
    project: str,
    save_id: str,
    *,
    messages: list[dict] | None = None,
    summaries: list[dict] | None = None,
    revision: int = 7,
) -> dict:
    return {
        "session_id": save_id,
        "name": save_id,
        "project": project,
        "revision": revision,
        "created_at": "2026-07-18T11:00:00",
        "updated_at": "2026-07-18T13:00:00",
        "message_history": list(messages or []),
        "summaries": list(summaries or []),
    }


class SearchApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.test_root = Path(self.tempdir.name) / "projects"
        self.old_session_root = session_manager.ROOT_DIR
        self.old_character_root = character_loader.ROOT_DIR
        session_manager.ROOT_DIR = self.test_root
        character_loader.ROOT_DIR = self.test_root
        session_manager.clear_session_stores_for_testing()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        self.client = httpx.AsyncClient(
            transport=transport,
            base_url=f"http://127.0.0.1:{PORT}",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        session_manager.ROOT_DIR = self.old_session_root
        character_loader.ROOT_DIR = self.old_character_root
        session_manager.clear_session_stores_for_testing()
        self.tempdir.cleanup()

    def write_session(self, project: str, save_id: str, payload: dict) -> Path:
        saves = self.test_root / project / "saves"
        saves.mkdir(parents=True, exist_ok=True)
        target = saves / f"{save_id}.json"
        target.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return target

    async def search(
        self,
        project: str,
        q: str,
        *,
        scope: str = "all",
        limit: int | str = 50,
    ) -> httpx.Response:
        return await self.client.get(
            "/api/search",
            params={"project": project, "q": q, "scope": scope, "limit": limit},
        )

    def assert_error_code(self, response: httpx.Response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(set(body), {"error"}, body)
        error = body["error"]
        self.assertIsInstance(error, dict, body)
        self.assertEqual(error.get("code"), code, body)

    def assert_top_level_contract(self, body: dict) -> None:
        self.assertEqual(set(body), {
            "project", "scope", "results", "total_matches", "truncated", "scanned", "skipped",
        })
        self.assertEqual(set(body["scanned"]), {"sessions", "sources", "bytes"})
        self.assertIs(type(body["total_matches"]), int)
        self.assertIs(type(body["truncated"]), bool)
        self.assertIsInstance(body["results"], list)
        self.assertIsInstance(body["skipped"], list)

    def assert_result_contract(self, result: dict) -> None:
        common = {"kind", "save_id", "revision", "time", "field", "snippet"}
        if result["kind"] == "message":
            self.assertEqual(set(result), common | {"message_id", "role", "pinned"})
            UUID(result["message_id"])
            self.assertIn(result["role"], {"user", "assistant"})
            self.assertIs(type(result["pinned"]), bool)
            self.assertEqual(result["field"], "content")
        else:
            self.assertEqual(result["kind"], "summary")
            self.assertEqual(set(result), common | {"summary_id"})
            UUID(result["summary_id"])
            self.assertIn(result["field"], {"text", "time", "facts", "relations"})
        self.assertIs(type(result["revision"]), int)
        self.assertEqual(set(result["snippet"]), {"prefix", "match", "suffix"})
        self.assertLessEqual(sum(len(value) for value in result["snippet"].values()), 240)

    async def test_input_validation_and_blank_query_never_acquires_storage_lock(self):
        project = "输入校验"
        (self.test_root / project / "saves").mkdir(parents=True)

        with patch.object(
            search_service.library_lock,
            "shared",
            side_effect=AssertionError("空查询禁止扫描存储"),
        ):
            response = await self.search(project, " \t\r\n ")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assert_top_level_contract(body)
        self.assertEqual(body, {
            "project": project,
            "scope": "all",
            "results": [],
            "total_matches": 0,
            "truncated": False,
            "scanned": {"sessions": 0, "sources": 0, "bytes": 0},
            "skipped": [],
        })

        invalid_cases = [
            ({"project": project, "q": "x" * 129}, "invalid_search_query"),
            ({"project": project, "q": "hit", "scope": "regex"}, "invalid_search_scope"),
            ({"project": project, "q": "hit", "limit": "0"}, "invalid_search_limit"),
            ({"project": project, "q": "hit", "limit": "101"}, "invalid_search_limit"),
            ({"project": project, "q": "hit", "limit": "1.5"}, "invalid_search_limit"),
        ]
        for params, code in invalid_cases:
            with self.subTest(params=params):
                invalid = await self.client.get("/api/search", params=params)
                self.assert_error_code(invalid, 400, code)

        missing = await self.search("不存在项目", "hit")
        self.assertEqual(missing.status_code, 404, missing.text)

    async def test_scopes_strict_fields_unicode_casefold_and_snippet_coordinates(self):
        project = "范围测试"
        long_content = "前" * 150 + "Straße" + "后" * 150 + " Straße"
        payload = session_payload(
            project,
            "存档B",
            messages=[
                message(MESSAGE_A, long_content, pinned=True),
                message(MESSAGE_B, "普通未固定消息", role="assistant"),
            ],
            summaries=[summary(
                text="摘要唯一词",
                time="群星历时间词",
                facts=["事实唯一词"],
                relations=["关系唯一词"],
            )],
        )
        self.write_session(project, "存档B", payload)
        self.write_session(
            project,
            "存档A",
            session_payload(
                project,
                "存档A",
                messages=[message(MESSAGE_C, "另一个 STRASSE 命中", pinned=False)],
                revision=3,
            ),
        )

        unicode_hit = await self.search(project, "STRASSE", scope="messages", limit=100)
        self.assertEqual(unicode_hit.status_code, 200, unicode_hit.text)
        body = unicode_hit.json()
        self.assert_top_level_contract(body)
        self.assertEqual(body["total_matches"], 2)
        self.assertEqual([item["save_id"] for item in body["results"]], ["存档A", "存档B"])
        for result in body["results"]:
            self.assert_result_contract(result)
        mapped = next(item for item in body["results"] if item["save_id"] == "存档B")
        self.assertEqual(mapped["snippet"]["match"], "Straße")
        self.assertLessEqual(sum(len(value) for value in mapped["snippet"].values()), 240)
        self.assertEqual(
            long_content.find(mapped["snippet"]["prefix"] + mapped["snippet"]["match"] + mapped["snippet"]["suffix"]),
            150 - len(mapped["snippet"]["prefix"]),
        )

        field_queries = [
            ("摘要唯一词", "text"),
            ("群星历时间词", "time"),
            ("事实唯一词", "facts"),
            ("关系唯一词", "relations"),
        ]
        for query, field in field_queries:
            with self.subTest(field=field):
                found = await self.search(project, query, scope="summaries")
                self.assertEqual(found.status_code, 200, found.text)
                self.assertEqual(found.json()["total_matches"], 1)
                result = found.json()["results"][0]
                self.assert_result_contract(result)
                self.assertEqual(result["field"], field)
                self.assertEqual(result["summary_id"], SUMMARY_A)

        pinned = await self.search(project, "Straße", scope="pinned")
        self.assertEqual(pinned.status_code, 200, pinned.text)
        self.assertEqual(len(pinned.json()["results"]), 1)
        self.assertTrue(pinned.json()["results"][0]["pinned"])

        summaries_only = await self.search(project, "普通未固定消息", scope="summaries")
        self.assertEqual(summaries_only.json()["results"], [])
        messages_only = await self.search(project, "摘要唯一词", scope="messages")
        self.assertEqual(messages_only.json()["results"], [])

        limited = await self.search(project, "strasse", scope="all", limit=1)
        self.assertEqual(limited.status_code, 200, limited.text)
        self.assertEqual(len(limited.json()["results"]), 1)
        self.assertEqual(limited.json()["total_matches"], 2)
        self.assertTrue(limited.json()["truncated"])
        identities = [
            (item["kind"], item["save_id"], item.get("message_id"), item.get("summary_id"), item["field"])
            for item in unicode_hit.json()["results"]
        ]
        self.assertEqual(len(identities), len(set(identities)), "all/messages 不得重复同一来源")

        forbidden_keys = {"path", "thinking", "metadata", "q", "query", "content", "body"}

        def walk(value):
            if isinstance(value, dict):
                self.assertTrue(forbidden_keys.isdisjoint(value), value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(body)

    async def test_redaction_precedes_matching_and_safe_hit_leaks_no_secret(self):
        project = "脱敏测试"
        secrets = {
            "bearer": "BearerSecretValue987654",
            "api_key": "ApiKeySecretValue987654",
            "password": "PasswordSecretValue987654",
            "token": "TokenSecretValue987654",
            "url_user": "UrlUserSecret987654",
            "url_password": "UrlPasswordSecret987654",
            "pem": "PemPrivateSecret987654",
            "truncated_pem": "MIIEvSecretMaterial987654",
            "bare": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "github_fine_grained": "github_pat_11AA0abcdefghijklmnopqrstuvwx1234567890ABCDE",
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "passphrase": "correct horse battery staple",
            "quoted_spaced_key": "ULTRA-SECRET-PASSPHRASE",
            "bracket_password": "bracket correct horse battery staple",
            "escaped_quote_tail": "LEAKED-SECRET-TAIL",
            "aws_temporary_key": "ASIAIOSFODNN7EXAMPLE",
            "sendgrid": "SG.abcdefghijklmnopqrstuv.abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            "telegram": "123456789:AAabcdefghijklmnopqrstuvwxyz0123456789",
        }
        content = (
            f"Authorization: Bearer {secrets['bearer']}；"
            f"api_key={secrets['api_key']}；"
            f"password: {secrets['password']}；"
            f"token = {secrets['token']}；"
            f"https://{secrets['url_user']}:{secrets['url_password']}@example.test/path；"
            f"passphrase: {secrets['passphrase']}；"
            f"\"api key\": \"{secrets['quoted_spaced_key']}\"；"
            f"os.environ[\"PASSWORD\"] = \"{secrets['bracket_password']}\"；"
            'client_secret="SAFE\\\"LEAKED-SECRET-TAIL"；'
            f"临时键 {secrets['aws_temporary_key']}；"
            f"邮件令牌 {secrets['sendgrid']}；"
            f"机器人令牌 {secrets['telegram']}；"
            "普通剧情命中；"
            "-----BEGIN PRIVATE KEY-----\n"
            f"{secrets['pem']}\n"
            "-----END PRIVATE KEY-----；"
            f"裸令牌 {secrets['bare']}"
            f"；细粒度令牌 {secrets['github_fine_grained']}"
            f"；AWS_SECRET_ACCESS_KEY={secrets['aws_secret_access_key']}"
        )
        self.write_session(
            project,
            "安全存档",
            session_payload(
                project,
                "安全存档",
                messages=[
                    message(MESSAGE_A, content),
                    message(
                        MESSAGE_B,
                        "-----BEGIN PRIVATE KEY-----\n"
                        f"{secrets['truncated_pem']}\n截断后普通剧情",
                    ),
                ],
            ),
        )

        for label, secret in secrets.items():
            with self.subTest(secret=label):
                response = await self.search(project, secret, scope="messages")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["total_matches"], 0, response.text)
                self.assertEqual(response.json()["results"], [])

        passphrase_fragment = await self.search(project, "horse battery", scope="messages")
        self.assertEqual(passphrase_fragment.status_code, 200, passphrase_fragment.text)
        self.assertEqual(passphrase_fragment.json()["total_matches"], 0)
        truncated_tail = await self.search(project, "截断后普通剧情", scope="messages")
        self.assertEqual(truncated_tail.status_code, 200, truncated_tail.text)
        self.assertEqual(truncated_tail.json()["total_matches"], 0)

        ordinary = await self.search(project, "普通剧情", scope="messages")
        self.assertEqual(ordinary.status_code, 200, ordinary.text)
        self.assertEqual(ordinary.json()["total_matches"], 1)
        snippet = ordinary.json()["results"][0]["snippet"]
        rendered = "".join((snippet["prefix"], snippet["match"], snippet["suffix"]))
        self.assertEqual(snippet["match"], "普通剧情")
        for secret in secrets.values():
            self.assertNotIn(secret, rendered)
        for word in ("correct", "horse", "battery", "staple"):
            self.assertNotIn(word, rendered)
        self.assertNotIn("BEGIN PRIVATE KEY", rendered)
        self.assertLessEqual(len(rendered), 240)

    async def test_history_is_ignored_and_corrupt_save_is_safe_skipped_without_quarantine(self):
        project = "损坏测试"
        saves = self.test_root / project / "saves"
        history = saves / ".history"
        history.mkdir(parents=True)
        self.write_session(
            project,
            "正常存档",
            session_payload(project, "正常存档", messages=[message(MESSAGE_A, "正常命中")]),
        )
        bad = saves / "损坏存档.json"
        bad.write_bytes(b'{"message_history": [ invalid')
        before = bad.read_bytes()
        (history / "正常存档.20260718_120000.json").write_text(
            json.dumps(session_payload(
                project,
                "正常存档",
                messages=[message(MESSAGE_B, "历史专属命中")],
            ), ensure_ascii=False),
            encoding="utf-8",
        )

        response = await self.search(project, "正常命中")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total_matches"], 1)
        self.assertEqual(len(body["skipped"]), 1)
        self.assertLessEqual(set(body["skipped"][0]), {"save_id", "code"})
        self.assertEqual(body["skipped"][0].get("save_id"), "损坏存档")
        self.assertNotIn(str(self.test_root), response.text)
        self.assertEqual(bad.read_bytes(), before)
        self.assertEqual(list(saves.rglob("*.corrupt*")), [])

        history_only = await self.search(project, "历史专属命中")
        self.assertEqual(history_only.status_code, 200, history_only.text)
        self.assertEqual(history_only.json()["results"], [])
        self.assertEqual(history_only.json()["total_matches"], 0)

    async def test_revision_must_round_trip_as_javascript_safe_integer(self):
        project = "安全整数版本"
        self.write_session(
            project,
            "正常存档",
            session_payload(project, "正常存档", messages=[message(MESSAGE_A, "安全整数命中")]),
        )
        self.write_session(
            project,
            "越界存档",
            session_payload(
                project,
                "越界存档",
                messages=[message(MESSAGE_B, "安全整数命中")],
                revision=(1 << 53) + 1,
            ),
        )

        response = await self.search(project, "安全整数命中", scope="messages")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total_matches"], 1)
        self.assertEqual(body["results"][0]["save_id"], "正常存档")
        self.assertEqual(
            body["skipped"],
            [{"save_id": "越界存档", "code": "session_corrupt"}],
        )
        self.assertNotIn(str((1 << 53) + 1), response.text)

    async def test_surrogate_save_is_skipped_and_route_response_remains_utf8_json(self):
        project = "Unicode标量"
        self.write_session(
            project,
            "正常存档",
            session_payload(project, "正常存档", messages=[message(MESSAGE_A, "契约命中正常")]),
        )
        unsafe = session_payload(
            project,
            "异常存档",
            messages=[message(MESSAGE_B, "契约命中\ud800秘密后文")],
        )
        saves = self.test_root / project / "saves"
        unsafe_path = saves / "异常存档.json"
        unsafe_path.write_bytes(
            json.dumps(unsafe, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        )

        response = await self.search(project, "契约命中", scope="messages")
        self.assertEqual(response.status_code, 200, response.text)
        response.content.decode("utf-8")
        body = response.json()
        self.assertEqual(body["total_matches"], 1)
        self.assertEqual(body["results"][0]["save_id"], "正常存档")
        self.assertEqual(
            body["skipped"],
            [{"save_id": "异常存档", "code": "session_corrupt"}],
        )
        self.assertNotIn("秘密后文", response.text)

    async def test_resource_limits_accept_equality_and_reject_plus_one_with_stable_413(self):
        self.assertEqual(search_service.MAX_SEARCH_SESSION_FILES, 500)
        self.assertEqual(search_service.MAX_SEARCH_SOURCES, 50_000)
        self.assertEqual(search_service.MAX_SEARCH_BYTES, 64 * 1024 * 1024)
        self.assertEqual(search_service.MAX_SEARCH_QUERY_LENGTH, 128)
        self.assertEqual(search_service.MAX_SEARCH_LIMIT, 100)
        self.assertEqual(search_service.MAX_SEARCH_SNIPPET_LENGTH, 240)
        project = "资源限制"
        first = self.write_session(
            project,
            "存档A",
            session_payload(
                project,
                "存档A",
                messages=[message(MESSAGE_A, "边界命中一"), message(MESSAGE_B, "边界命中二")],
            ),
        )
        second = self.write_session(
            project,
            "存档B",
            session_payload(project, "存档B", messages=[]),
        )

        with patch.object(search_service, "MAX_SEARCH_SESSION_FILES", 2):
            equal_files = await self.search(project, "边界命中", scope="messages")
            self.assertEqual(equal_files.status_code, 200, equal_files.text)
            self.write_session(project, "存档C", session_payload(project, "存档C"))
            extra_file = await self.search(project, "边界命中", scope="messages")
            self.assert_error_code(extra_file, 413, "search_file_limit_exceeded")
        (self.test_root / project / "saves" / "存档C.json").unlink()

        with patch.object(search_service, "MAX_SEARCH_SOURCES", 2):
            equal_sources = await self.search(project, "边界命中", scope="messages")
            self.assertEqual(equal_sources.status_code, 200, equal_sources.text)
            payload = session_payload(
                project,
                "存档A",
                messages=[
                    message(MESSAGE_A, "边界命中一"),
                    message(MESSAGE_B, "边界命中二"),
                    message(MESSAGE_C, "边界命中三"),
                ],
            )
            self.write_session(project, "存档A", payload)
            extra_source = await self.search(project, "边界命中", scope="messages")
            self.assert_error_code(extra_source, 413, "search_source_limit_exceeded")

        self.write_session(
            project,
            "存档A",
            session_payload(project, "存档A", messages=[message(MESSAGE_A, "字节边界命中")]),
        )
        byte_total = first.stat().st_size + second.stat().st_size
        with patch.object(search_service, "MAX_SEARCH_BYTES", byte_total):
            equal_bytes = await self.search(project, "字节边界", scope="messages")
            self.assertEqual(equal_bytes.status_code, 200, equal_bytes.text)
        with patch.object(search_service, "MAX_SEARCH_BYTES", byte_total - 1):
            extra_byte = await self.search(project, "字节边界", scope="messages")
            self.assert_error_code(extra_byte, 413, "search_byte_limit_exceeded")

    async def test_scan_holds_one_library_shared_lock_and_reads_each_save_once(self):
        project = "并发读取"
        for save_id, message_id in (("存档A", MESSAGE_A), ("存档B", MESSAGE_B)):
            self.write_session(
                project,
                save_id,
                session_payload(project, save_id, messages=[message(message_id, "锁内命中")]),
            )

        events: list[str] = []
        original_read = search_service.SearchService._read_snapshot_bytes

        @contextmanager
        def tracked_shared():
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        def tracked_read(service, path, byte_budget):
            self.assertEqual(events[:1], ["enter"])
            self.assertNotIn("exit", events)
            events.append(f"read:{path.stem}")
            return original_read(service, path, byte_budget)

        with (
            patch.object(search_service.library_lock, "shared", tracked_shared),
            patch.object(search_service.SearchService, "_read_snapshot_bytes", tracked_read),
        ):
            response = await self.search(project, "锁内命中")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(events, ["enter", "read:存档A", "read:存档B", "exit"])
        self.assertEqual(response.json()["scanned"]["sessions"], 2)

    async def test_atomic_replace_cannot_bypass_same_snapshot_byte_limit(self):
        project = "原子替换竞态"
        target = self.write_session(
            project,
            "存档A",
            session_payload(project, "存档A", messages=[message(MESSAGE_A, "旧命中")]),
        )
        old_size = target.stat().st_size
        replacement = target.with_suffix(".swap")
        replacement.write_text(
            json.dumps(
                session_payload(
                    project,
                    "存档A",
                    messages=[message(MESSAGE_A, "新命中" + "扩" * (old_size * 4))],
                    revision=8,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        self.assertGreater(replacement.stat().st_size, old_size)

        original_open = Path.open
        swapped = False

        def replace_between_stat_and_open(path, *args, **kwargs):
            nonlocal swapped
            if path == target and not swapped:
                swapped = True
                replacement.replace(target)
            return original_open(path, *args, **kwargs)

        with (
            patch.object(Path, "open", replace_between_stat_and_open),
            patch.object(search_service, "MAX_SEARCH_BYTES", old_size),
        ):
            response = await self.search(project, "新命中", scope="messages")

        self.assertTrue(swapped)
        self.assert_error_code(response, 413, "search_byte_limit_exceeded")

    async def test_non_regular_json_candidates_cannot_bypass_file_limit(self):
        project = "候选项上限"
        saves = self.test_root / project / "saves"
        saves.mkdir(parents=True)
        for name in ("a.json", "b.json", "c.json"):
            (saves / name).mkdir()

        with patch.object(search_service, "MAX_SEARCH_SESSION_FILES", 2):
            response = await self.search(project, "命中")

        self.assert_error_code(response, 413, "search_file_limit_exceeded")

    def test_large_literal_match_has_bounded_coordinate_memory(self):
        text = "a" * (1024 * 1024)
        redacted, spans = search_service._redact_credentials(text)
        self.assertIs(redacted, text)
        self.assertEqual(spans, ())

        tracemalloc.start()
        try:
            self.assertIsNone(search_service._literal_match(text, "z"))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_dense_credentials_are_fully_redacted_with_bounded_memory(self):
        text = "token=a;" * (1024 * 128)
        tracemalloc.start()
        try:
            redacted, spans = search_service._redact_credentials(text)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(redacted, "[凭据已隐藏]")
        self.assertEqual(spans, ((0, len(redacted)),))
        self.assertLess(peak, 8 * 1024 * 1024)
        self.assertIsNone(search_service._literal_match(text, "token"))


if __name__ == "__main__":
    unittest.main()
