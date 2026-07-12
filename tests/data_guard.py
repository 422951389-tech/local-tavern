"""真实数据只读清单工具，供 pytest 会话与外部验收共用。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def file_manifest(root: Path) -> dict[str, str]:
    root = root.resolve(strict=False)
    if not root.exists():
        return {}
    files = sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda path: str(path.relative_to(root)),
    )
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest().upper()
        for path in files
    }


def manifest_digest(manifest: dict[str, str]) -> str:
    rows = [f"{name}\t{digest}" for name, digest in sorted(manifest.items())]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest().upper()


def manifest_diff(
    before: dict[str, str],
    after: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    changed = sorted(
        name for name in before.keys() & after.keys()
        if before[name] != after[name]
    )
    return added, removed, changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="输出目录的逐文件 SHA-256 清单摘要")
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=False)
    manifest = file_manifest(root)
    print(json.dumps({
        "root": str(root),
        "files": len(manifest),
        "manifest_sha256": manifest_digest(manifest),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
