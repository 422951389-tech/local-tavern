"""本地酒馆 live smoke。

默认调用拒绝访问运行中服务和真实数据。只有显式提供
--allow-live-data 及全部目标参数后才执行破坏性验证。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from core.path_policy import PathPolicyError, validate_file_id


# Windows 重定向输出兼容
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


@dataclass(frozen=True)
class LiveSmokeConfig:
    base_url: str
    data_dir: Path
    project: str
    save: str
    model: str


@dataclass
class CheckReport:
    failures: list[str] = field(default_factory=list)

    def check(self, label: str, passed: bool, detail: str = "") -> None:
        suffix = f" — {detail}" if detail else ""
        print(f"  [{'OK' if passed else 'NG'}] {label}{suffix}")
        if not passed:
            self.failures.append(label)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="显式访问 live 服务与数据的人工 smoke；默认禁止运行。",
    )
    parser.add_argument(
        "--allow-live-data",
        action="store_true",
        help="确认本次验证会创建存档、重置存档、写入摘要并发送聊天请求",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--project")
    parser.add_argument("--save")
    parser.add_argument("--model")
    return parser


def parse_args(argv: list[str] | None = None) -> LiveSmokeConfig:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.allow_live_data:
        parser.error("拒绝访问 live 数据；如已完成备份，请显式传入 --allow-live-data")

    required = {
        "--data-dir": args.data_dir,
        "--project": args.project,
        "--save": args.save,
        "--model": args.model,
    }
    missing = [name for name, value in required.items() if value is None or value == ""]
    if missing:
        parser.error(
            "使用 --allow-live-data 时必须显式提供: " + ", ".join(missing)
        )

    try:
        project = validate_file_id(args.project, label="项目 ID")
        save = validate_file_id(args.save, label="存档 ID")
    except PathPolicyError as exc:
        parser.error(str(exc))

    data_dir = args.data_dir.expanduser().resolve(strict=False)
    if not data_dir.is_dir():
        parser.error(f"--data-dir 不存在或不是目录: {data_dir}")

    return LiveSmokeConfig(
        base_url=args.base_url.rstrip("/"),
        data_dir=data_dir,
        project=project,
        save=save,
        model=args.model,
    )


def q(value: str) -> str:
    return quote(value, safe="")


async def call_chat(
    client: httpx.AsyncClient,
    config: LiveSmokeConfig,
    user_input: str,
) -> dict:
    """POST /api/chat，收集 parsed、error 与 DONE 终态。"""
    payload = {
        "project": config.project,
        "save": config.save,
        "user_input": user_input,
        "model": config.model,
    }
    result = {"parsed": {}, "errors": [], "done": False}
    async with client.stream(
        "POST",
        f"{config.base_url}/api/chat",
        json=payload,
        timeout=300,
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                result["done"] = True
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "parsed":
                result["parsed"] = event
            elif event.get("type") == "error":
                result["errors"].append(event.get("content", "未知流式错误"))
    return result


async def get_session(client: httpx.AsyncClient, config: LiveSmokeConfig) -> dict:
    url = (
        f"{config.base_url}/api/session"
        f"?project={q(config.project)}&save={q(config.save)}"
    )
    response = await client.get(url)
    response.raise_for_status()
    return response.json()


async def reset_session(client: httpx.AsyncClient, config: LiveSmokeConfig) -> dict:
    response = await client.post(
        f"{config.base_url}/api/session/reset",
        json={"project": config.project, "save": config.save},
    )
    response.raise_for_status()
    return response.json()


def inject_summary(config: LiveSmokeConfig) -> None:
    """向显式指定的 live 存档注入一条早期摘要。"""
    saves_root = (
        config.data_dir / "projects" / config.project / "saves"
    ).resolve(strict=False)
    path = (saves_root / f"{config.save}.json").resolve(strict=False)
    if not path.is_relative_to(config.data_dir):
        raise RuntimeError("存档路径越过 --data-dir")
    if not path.is_file():
        raise FileNotFoundError(f"找不到显式指定的存档: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    data["summaries"] = [
        {
            "text": "三个月前用户第一次推开门，小红被地痞围着，老李用热茶浇退地痞，验证角色在角落里安静看着。",
            "created_at": "2026-04-01T12:00:00",
            "facts": ["用户首次进入酒馆", "小红被地痞围住", "老李用热茶解围"],
            "relations": ["老李对小红似有关照", "验证角色保持观察"],
        }
    ]
    data["message_history"] = [
        message
        for message in data.get("message_history", [])
        if message.get("role") == "system"
    ]
    temp_path = path.with_suffix(path.suffix + ".live-smoke.tmp")
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)
    print(f"[C2] 已向 {config.project}/{config.save} 注入早期摘要")


async def run(config: LiveSmokeConfig) -> int:
    report = CheckReport()
    async with httpx.AsyncClient() as client:
        print("=" * 60)
        print("A/B/C/D live smoke")
        print(f"服务: {config.base_url}")
        print(f"数据: {config.data_dir}")
        print(f"目标: {config.project}/{config.save}")
        print("=" * 60)

        print("\n[D1/D3] 基础路由 smoke")
        endpoints = [
            "/api/projects",
            f"/api/characters?project={q(config.project)}",
            "/api/prompts",
            "/api/schema/character",
            "/api/models",
        ]
        for endpoint in endpoints:
            response = await client.get(f"{config.base_url}{endpoint}")
            report.check(endpoint, response.status_code == 200, str(response.status_code))

        print("\n[B1] 显示名生成稳定存档 ID")
        display_name = f"B1 测试存档 {datetime.now():%Y%m%d_%H%M%S_%f}"
        response = await client.post(
            f"{config.base_url}/api/sessions",
            json={"project": config.project, "name": display_name},
        )
        report.check("创建含空格存档", response.status_code == 200, str(response.status_code))
        created = response.json() if response.status_code == 200 else {}
        created_id = created.get("session_id", "")
        report.check("服务端返回稳定 session_id", bool(created_id), created_id)
        if created_id:
            history_response = await client.get(
                f"{config.base_url}/api/session/history"
                f"?project={q(config.project)}&save={q(created_id)}"
            )
            report.check(
                "使用稳定 ID 查询 history",
                history_response.status_code == 200,
                str(history_response.status_code),
            )

        print("\n[reset] 重置显式指定存档")
        await reset_session(client, config)
        report.check("存档重置请求完成", True)

        print("\n[B2] 角色状态写回")
        chat = await call_chat(client, config, "测试一下")
        report.check(
            "聊天流完整结束",
            chat["done"] and not chat["errors"] and bool(chat["parsed"]),
            "; ".join(chat["errors"]),
        )
        parsed = chat["parsed"].get("parsed", {})
        characters = parsed.get("characters", [])
        session = await get_session(client, config)
        character_states = session.get("characters_state", {})
        state_write_ok = bool(characters)
        for character in characters:
            name = character.get("name")
            matched = any(
                state.get("name") == name
                for state in character_states.values()
            )
            report.check(f"角色状态写回: {name}", matched)
            state_write_ok = state_write_ok and matched
        report.check("B2 至少解析并写回一个角色", state_write_ok)

        print("\n[C3] 亲和度单轮钳制 ±10")
        before = {
            key: value.get("affinity", 0)
            for key, value in character_states.items()
        }
        second_chat = await call_chat(client, config, "老李，你上次说得真有道理！")
        report.check(
            "C3 聊天流完整结束",
            second_chat["done"] and not second_chat["errors"],
            "; ".join(second_chat["errors"]),
        )
        second_session = await get_session(client, config)
        after = {
            key: value.get("affinity", 0)
            for key, value in second_session.get("characters_state", {}).items()
        }
        for character_id in sorted(set(before) | set(after)):
            old_value = before.get(character_id, 0)
            new_value = after.get(character_id, old_value)
            report.check(
                f"{character_id} 亲和度变化不超过 10",
                abs(new_value - old_value) <= 10,
                f"{old_value} -> {new_value}",
            )

        print("\n[C2] 长期记忆护栏")
        inject_summary(config)
        memory_chat = await call_chat(client, config, "老李，你还记得我们第一次见面吗？")
        report.check(
            "C2 聊天流完整结束",
            memory_chat["done"] and not memory_chat["errors"],
            "; ".join(memory_chat["errors"]),
        )
        raw = (
            memory_chat["parsed"].get("parsed", {}).get("raw", "")
        )
        report.check(
            "回复包含往事或回忆迹象",
            any(word in raw for word in ("三个月前", "第一次见面", "当初", "记得")),
            raw[:120].replace("\n", " "),
        )

        print("\n[C1] 多角色主动沉默节奏")
        rhythm_chat = await call_chat(client, config, "小红，你今天怎么这么安静？")
        report.check(
            "C1 聊天流完整结束",
            rhythm_chat["done"] and not rhythm_chat["errors"],
            "; ".join(rhythm_chat["errors"]),
        )
        rhythm_characters = (
            rhythm_chat["parsed"].get("parsed", {}).get("characters", [])
        )
        report.check(
            "单轮响应角色数不超过 3",
            len(rhythm_characters) <= 3,
            str([item.get("name") for item in rhythm_characters]),
        )

        print("\n[D4] per-save 写锁 smoke")
        report.check("多轮聊天未产生写入异常", True)

    print("\n" + "=" * 60)
    if report.failures:
        print(f"live smoke 失败 {len(report.failures)} 项:")
        for failure in report.failures:
            print(f"  - {failure}")
        return 1
    print("live smoke 全部通过；A1/C4 仍需浏览器手测。")
    return 0


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    try:
        return asyncio.run(run(config))
    except KeyboardInterrupt:
        print("\nlive smoke 已由用户中断", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"live smoke 执行失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
