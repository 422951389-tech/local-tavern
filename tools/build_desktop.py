"""构建并核验无端口 Windows 桌面发行目录。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from importlib import metadata
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "packaging" / "LocalTavern.spec"
APP_NAME = "LocalTavern"
FORBIDDEN_RELEASE_NAMES = {"backups", "data", "logs", "provider-secrets.json"}
MANIFEST_NAME = "release-manifest.json"
SBOM_NAME = "release-sbom.cdx.json"
MANIFEST_SCHEMA_VERSION = 2
CYCLONEDX_SPEC_VERSION = "1.5"
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_THUMBPRINT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BUILD_INPUT_DIRECTORIES = ("core", "desktop", "routes", "web", "prompts", "packaging")
BUILD_INPUT_FILES = (
    "server.py",
    "requirements.lock.txt",
    "requirements.hashes.txt",
    "requirements-desktop.lock.txt",
    "requirements-desktop.hashes.txt",
    "package.json",
    "package-lock.json",
    "tools/build_desktop.py",
    "tools/dependency_locks.py",
    "tools/node_dependency_lock.py",
)
PROTECTED_BUILD_TOP_LEVEL = {
    ".codegraph",
    ".git",
    ".venv",
    ".venv-desktop",
    ".venv-dev",
    "artifacts",
    "backups",
    "core",
    "data",
    "desktop",
    "docs",
    "logs",
    "node_modules",
    "packaging",
    "prompts",
    "routes",
    "rules",
    "tests",
    "tools",
    "web",
}


class DesktopBuildError(RuntimeError):
    pass


def _inside_root(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=False)
    if resolved == root or not resolved.is_relative_to(root):
        raise DesktopBuildError(
            f"构建目录必须位于项目内且不能等于项目根目录：{resolved}"
        )
    relative = resolved.relative_to(root)
    if relative.parts[0].casefold() in PROTECTED_BUILD_TOP_LEVEL:
        raise DesktopBuildError(f"构建目录不能位于受保护的项目目录：{resolved}")
    return resolved


def _remove_generated(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _command_output(command: Sequence[str], *, root: Path) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            list(command),
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    output = (completed.stdout or completed.stderr or "").strip()
    return completed.returncode, output


def _source_provenance(root: Path) -> dict[str, object]:
    commit_code, commit_output = _command_output(
        ("git", "rev-parse", "--verify", "HEAD"),
        root=root,
    )
    commit = commit_output.splitlines()[0] if commit_output else ""
    status_code, status_output = _command_output(
        ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        root=root,
    )
    available = commit_code == 0 and _COMMIT_RE.fullmatch(commit) is not None
    return {
        "vcs": "git",
        "available": available and status_code == 0,
        "commit": commit.lower() if available else None,
        "dirty": bool(status_output) if status_code == 0 else None,
    }


def _distribution_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _toolchain_versions(root: Path) -> dict[str, object]:
    node_code, node_output = _command_output(("node", "--version"), root=root)
    npm_code, npm_output = _command_output(("npm", "--version"), root=root)
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "node": node_output.splitlines()[0] if node_code == 0 and node_output else None,
        "npm": npm_output.splitlines()[0] if npm_code == 0 and npm_output else None,
        "pyinstaller": _distribution_version("pyinstaller"),
        "pyside6": _distribution_version("PySide6"),
        "platform": platform.platform(),
    }


def _build_input_paths(root: Path) -> list[Path]:
    paths: set[Path] = set()
    for name in BUILD_INPUT_DIRECTORIES:
        directory = root / name
        if not directory.is_dir():
            continue
        paths.update(
            path
            for path in directory.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix.casefold() not in {".pyc", ".pyo"}
        )
    paths.update(path for name in BUILD_INPUT_FILES if (path := root / name).is_file())
    return sorted(paths, key=lambda item: item.relative_to(root).as_posix())


def _build_input_manifest(root: Path) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    aggregate = hashlib.sha256()
    for path in _build_input_paths(root):
        relative = path.relative_to(root).as_posix()
        digest = _sha256(path)
        rows.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": digest,
            }
        )
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(bytes.fromhex(digest))
        aggregate.update(b"\n")
    if not rows:
        raise DesktopBuildError("没有找到可哈希的桌面构建输入")
    return {
        "algorithm": "SHA-256",
        "file_count": len(rows),
        "aggregate_sha256": aggregate.hexdigest().upper(),
        "files": rows,
    }


def _release_manifest(app_dir: Path) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    total_bytes = 0
    files = (item for item in app_dir.rglob("*") if item.is_file())
    for path in sorted(files, key=lambda item: item.relative_to(app_dir).as_posix()):
        size = path.stat().st_size
        total_bytes += size
        rows.append(
            {
                "path": path.relative_to(app_dir).as_posix(),
                "bytes": size,
                "sha256": _sha256(path),
            }
        )
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "application": APP_NAME,
        "packaging": "pyinstaller-onedir",
        "file_count": len(rows),
        "total_bytes": total_bytes,
        "files": rows,
    }


def _parse_python_lock(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        raise DesktopBuildError(f"SBOM 缺少 Python 锁文件：{path.name}")
    packages: list[tuple[str, str]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("==") != 1:
            raise DesktopBuildError(f"SBOM 无法解析 {path.name} 第 {line_number} 行")
        name, version = line.split("==", 1)
        if not name or not version:
            raise DesktopBuildError(f"SBOM 无法解析 {path.name} 第 {line_number} 行")
        packages.append((name, version))
    if not packages:
        raise DesktopBuildError("SBOM 的 Python 依赖集合为空")
    return packages


def _integrity_hash(value: object) -> list[dict[str, str]]:
    if not isinstance(value, str) or not value.startswith("sha512-"):
        return []
    try:
        raw = base64.b64decode(value.removeprefix("sha512-"), validate=True)
    except (ValueError, base64.binascii.Error):
        return []
    return [{"alg": "SHA-512", "content": raw.hex().upper()}]


def _node_package_name(lock_path: str, value: Mapping[str, object]) -> str | None:
    explicit = value.get("name")
    if isinstance(explicit, str) and explicit:
        return explicit
    marker = "node_modules/"
    if marker not in lock_path:
        return None
    return lock_path.rsplit(marker, 1)[1]


def _node_components(root: Path) -> list[dict[str, object]]:
    path = root / "package-lock.json"
    if not path.is_file():
        raise DesktopBuildError("SBOM 缺少 package-lock.json")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DesktopBuildError("SBOM 无法读取 package-lock.json") from exc
    packages = document.get("packages") if isinstance(document, dict) else None
    if not isinstance(packages, dict):
        raise DesktopBuildError("SBOM 的 package-lock.json 缺少 packages")
    components: list[dict[str, object]] = []
    for lock_path in sorted(packages):
        value = packages[lock_path]
        if not lock_path or not isinstance(value, dict):
            continue
        name = _node_package_name(lock_path, value)
        version = value.get("version")
        if not name or not isinstance(version, str) or not version:
            continue
        encoded_name = quote(name, safe="/")
        component: dict[str, object] = {
            "type": "library",
            "bom-ref": f"pkg:npm/{encoded_name}@{version}",
            "name": name,
            "version": version,
            "purl": f"pkg:npm/{encoded_name}@{version}",
            "scope": "excluded",
            "properties": [
                {"name": "local-tavern:role", "value": "frontend-build-test-tool"},
                {"name": "local-tavern:lock-path", "value": lock_path},
            ],
        }
        hashes = _integrity_hash(value.get("integrity"))
        if hashes:
            component["hashes"] = hashes
        license_name = value.get("license")
        if isinstance(license_name, str) and license_name:
            component["licenses"] = [{"license": {"id": license_name}}]
        components.append(component)
    return components


def _python_components(root: Path) -> list[dict[str, object]]:
    components: list[dict[str, object]] = []
    for name, version in _parse_python_lock(root / "requirements-desktop.lock.txt"):
        normalized = re.sub(r"[-_.]+", "-", name).casefold()
        purl = f"pkg:pypi/{normalized}@{version}"
        components.append(
            {
                "type": "library",
                "bom-ref": purl,
                "name": name,
                "version": version,
                "purl": purl,
                "scope": "required",
                "properties": [
                    {
                        "name": "local-tavern:source",
                        "value": "requirements-desktop.lock.txt",
                    }
                ],
            }
        )
    return components


def _frontend_asset_components(app_dir: Path) -> list[dict[str, object]]:
    web_root = app_dir / "_internal" / "web"
    components: list[dict[str, object]] = []
    for path in sorted(
        (item for item in web_root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(web_root).as_posix(),
    ):
        relative = path.relative_to(web_root).as_posix()
        bom_ref = f"urn:local-tavern:web-asset:{quote(relative, safe='/')}"
        components.append(
            {
                "type": "file",
                "bom-ref": bom_ref,
                "name": f"web/{relative}",
                "hashes": [{"alg": "SHA-256", "content": _sha256(path)}],
                "properties": [
                    {"name": "local-tavern:role", "value": "bundled-frontend-asset"},
                    {"name": "local-tavern:bytes", "value": str(path.stat().st_size)},
                ],
            }
        )
    if not components:
        raise DesktopBuildError("SBOM 没有找到已打包的前端本地资产")
    return components


def generate_sbom(
    *,
    root: Path,
    app_dir: Path,
    build_inputs: Mapping[str, object],
    source: Mapping[str, object],
    signature: Mapping[str, object],
) -> dict[str, object]:
    components = [
        *_python_components(root),
        *_node_components(root),
        *_frontend_asset_components(app_dir),
    ]
    aggregate = str(build_inputs["aggregate_sha256"])
    application_ref = "pkg:generic/local-tavern@standalone"
    return {
        "bomFormat": "CycloneDX",
        "specVersion": CYCLONEDX_SPEC_VERSION,
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'local-tavern:{aggregate}')}",
        "version": 1,
        "metadata": {
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "LocalTavern offline SBOM generator",
                        "version": "1",
                    }
                ]
            },
            "component": {
                "type": "application",
                "bom-ref": application_ref,
                "name": APP_NAME,
                "version": "standalone",
                "properties": [
                    {
                        "name": "local-tavern:source-commit",
                        "value": str(source.get("commit") or "unavailable"),
                    },
                    {
                        "name": "local-tavern:source-dirty",
                        "value": str(source.get("dirty")).lower(),
                    },
                    {
                        "name": "local-tavern:authenticode-status",
                        "value": str(signature["status"]),
                    },
                    {
                        "name": "local-tavern:build-input-sha256",
                        "value": aggregate,
                    },
                ],
            },
        },
        "components": components,
        "dependencies": [
            {
                "ref": application_ref,
                "dependsOn": [
                    component["bom-ref"]
                    for component in components
                    if component.get("scope") != "excluded"
                ],
            }
        ],
    }


def _resolve_signtool(value: Path | None) -> Path:
    if value is not None:
        candidate = value.expanduser().resolve(strict=False)
        if candidate.is_file():
            return candidate
        raise DesktopBuildError("指定的 signtool.exe 不存在")
    found = shutil.which("signtool.exe") or shutil.which("signtool")
    if found:
        return Path(found).resolve()
    raise DesktopBuildError("已请求 Authenticode 签名，但没有找到 signtool.exe")


def _sign_executable(
    executable: Path,
    *,
    certificate: Path | None,
    thumbprint: str | None,
    password_env: str | None,
    timestamp_url: str | None,
    signtool: Path | None,
    machine_store: bool,
) -> dict[str, object]:
    if certificate is not None and thumbprint is not None:
        raise DesktopBuildError("签名证书文件与证书存储指纹不能同时使用")
    if certificate is None and thumbprint is None:
        if any((password_env, timestamp_url, signtool, machine_store)):
            raise DesktopBuildError("签名参数缺少证书来源")
        return {
            "status": "unsigned",
            "authenticode_verified": False,
            "reason": "certificate_not_provided",
        }
    if timestamp_url is not None and not timestamp_url.casefold().startswith(
        "https://"
    ):
        raise DesktopBuildError("Authenticode 时间戳地址必须使用 HTTPS")

    tool = _resolve_signtool(signtool)
    command = [str(tool), "sign", "/fd", "SHA256"]
    metadata_row: dict[str, object]
    if certificate is not None:
        if machine_store:
            raise DesktopBuildError("PFX 证书签名不接受机器证书存储参数")
        cert = certificate.expanduser().resolve(strict=False)
        if not cert.is_file():
            raise DesktopBuildError("Authenticode 证书文件不存在")
        command.extend(["/f", str(cert)])
        if password_env is not None:
            if _ENVIRONMENT_NAME_RE.fullmatch(password_env) is None:
                raise DesktopBuildError("签名密码环境变量名无效")
            password = os.environ.get(password_env)
            if password is None:
                raise DesktopBuildError("签名密码环境变量未设置")
            command.extend(["/p", password])
        metadata_row = {
            "method": "pfx",
            "certificate_sha256": _sha256(cert),
        }
    else:
        normalized = re.sub(r"\s+", "", thumbprint or "")
        if _THUMBPRINT_RE.fullmatch(normalized) is None:
            raise DesktopBuildError("证书存储指纹必须是 40 位十六进制 SHA-1")
        if password_env is not None:
            raise DesktopBuildError("证书存储签名不接受 PFX 密码参数")
        command.extend(["/sha1", normalized])
        if machine_store:
            command.append("/sm")
        metadata_row = {
            "method": "certificate_store",
            "certificate_thumbprint_sha256": hashlib.sha256(
                normalized.upper().encode("ascii")
            )
            .hexdigest()
            .upper(),
            "machine_store": machine_store,
        }
    if timestamp_url is not None:
        command.extend(["/tr", timestamp_url, "/td", "SHA256"])
    command.append(str(executable))

    completed = subprocess.run(command, check=False, text=True)
    if completed.returncode != 0:
        raise DesktopBuildError(f"Authenticode 签名失败：退出码 {completed.returncode}")
    verified = subprocess.run(
        [str(tool), "verify", "/pa", "/all", str(executable)],
        check=False,
        text=True,
    )
    if verified.returncode != 0:
        raise DesktopBuildError(
            f"Authenticode 签名验证失败：退出码 {verified.returncode}"
        )
    return {
        "status": "signed",
        "authenticode_verified": True,
        "timestamped": timestamp_url is not None,
        **metadata_row,
    }


def _find_qt_webengine_process(app_dir: Path) -> Path | None:
    return next(app_dir.rglob("QtWebEngineProcess.exe"), None)


def verify_release(app_dir: Path) -> dict[str, object]:
    executable = app_dir / f"{APP_NAME}.exe"
    if not executable.is_file():
        raise DesktopBuildError(f"桌面可执行文件不存在：{executable}")
    internal = app_dir / "_internal"
    required = (
        internal / "web" / "index.html",
        internal / "prompts" / "system.md",
        internal / "prompts" / "summary.md",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise DesktopBuildError(f"发行目录缺少资源：{missing}")
    if _find_qt_webengine_process(app_dir) is None:
        raise DesktopBuildError("发行目录缺少 QtWebEngineProcess.exe")
    leaked = sorted(
        path.relative_to(app_dir).as_posix()
        for path in app_dir.rglob("*")
        if path.name.casefold() in FORBIDDEN_RELEASE_NAMES
    )
    if leaked:
        raise DesktopBuildError(f"发行目录包含禁止打包的用户数据：{leaked}")
    return _release_manifest(app_dir)


def build(
    *,
    output_dir: Path,
    work_dir: Path,
    clean: bool,
    sign_certificate: Path | None = None,
    sign_thumbprint: str | None = None,
    sign_password_env: str | None = None,
    timestamp_url: str | None = None,
    signtool: Path | None = None,
    sign_machine_store: bool = False,
) -> dict[str, object]:
    root = ROOT.resolve()
    output = _inside_root(output_dir, root)
    work = _inside_root(work_dir, root)
    if output == work or output.is_relative_to(work) or work.is_relative_to(output):
        raise DesktopBuildError("发行目录与构建工作目录不能重叠")

    lock_check = subprocess.run(
        [sys.executable, "tools/dependency_locks.py", "--environment", "desktop"],
        cwd=root,
        text=True,
        check=False,
    )
    if lock_check.returncode != 0:
        raise DesktopBuildError("桌面构建环境与锁文件不一致")

    source = _source_provenance(root)
    toolchain = _toolchain_versions(root)
    build_inputs = _build_input_manifest(root)
    if clean:
        _remove_generated(output)
        _remove_generated(work)
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    for stale_name in (MANIFEST_NAME, SBOM_NAME):
        stale = output / stale_name
        if stale.is_file():
            stale.unlink()

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(output),
        "--workpath",
        str(work),
        str(SPEC_PATH),
    ]
    completed = subprocess.run(command, cwd=root, text=True, check=False)
    if completed.returncode != 0:
        raise DesktopBuildError(f"PyInstaller 构建失败：退出码 {completed.returncode}")
    final_build_inputs = _build_input_manifest(root)
    if final_build_inputs["aggregate_sha256"] != build_inputs["aggregate_sha256"]:
        raise DesktopBuildError("桌面构建期间源输入发生变化；拒绝生成失真 manifest")

    app_dir = output / APP_NAME
    executable = app_dir / f"{APP_NAME}.exe"
    verify_release(app_dir)
    signature = _sign_executable(
        executable,
        certificate=sign_certificate,
        thumbprint=sign_thumbprint,
        password_env=sign_password_env,
        timestamp_url=timestamp_url,
        signtool=signtool,
        machine_store=sign_machine_store,
    )
    manifest = verify_release(app_dir)
    sbom = generate_sbom(
        root=root,
        app_dir=app_dir,
        build_inputs=build_inputs,
        source=source,
        signature=signature,
    )
    sbom_path = output / SBOM_NAME
    _write_json(sbom_path, sbom)
    manifest.update(
        {
            "source": source,
            "toolchain": toolchain,
            "build_inputs": build_inputs,
            "signature": signature,
            "sbom": {
                "format": "CycloneDX",
                "spec_version": CYCLONEDX_SPEC_VERSION,
                "path": SBOM_NAME,
                "sha256": _sha256(sbom_path),
            },
        }
    )
    manifest_path = output / MANIFEST_NAME
    _write_json(manifest_path, manifest)
    return {
        "status": "passed",
        "application_dir": str(app_dir),
        "executable": str(executable),
        "manifest": str(manifest_path),
        "sbom": str(sbom_path),
        "signature_status": signature["status"],
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建本地酒馆独立桌面发行目录")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release")
    parser.add_argument("--work-dir", type=Path, default=ROOT / ".desktop-build")
    parser.add_argument("--clean", action="store_true")
    certificate = parser.add_mutually_exclusive_group()
    certificate.add_argument(
        "--sign-certificate",
        type=Path,
        help="PFX/P12 证书路径；密码只通过 --sign-password-env 读取",
    )
    certificate.add_argument(
        "--sign-thumbprint",
        help="Windows 证书存储中的 40 位 SHA-1 指纹",
    )
    parser.add_argument(
        "--sign-password-env",
        help="PFX 密码所在环境变量名；不会写入 manifest",
    )
    parser.add_argument("--timestamp-url", help="HTTPS RFC3161 时间戳服务")
    parser.add_argument("--signtool", type=Path, help="signtool.exe 的显式路径")
    parser.add_argument(
        "--sign-machine-store",
        action="store_true",
        help="从 LocalMachine 证书存储按指纹签名",
    )
    args = parser.parse_args(argv)
    try:
        result = build(
            output_dir=args.output_dir,
            work_dir=args.work_dir,
            clean=args.clean,
            sign_certificate=args.sign_certificate,
            sign_thumbprint=args.sign_thumbprint,
            sign_password_env=args.sign_password_env,
            timestamp_url=args.timestamp_url,
            signtool=args.signtool,
            sign_machine_store=args.sign_machine_store,
        )
    except DesktopBuildError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
