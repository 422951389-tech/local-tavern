from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import build_desktop
from tools.build_desktop import (
    DesktopBuildError,
    _build_input_manifest,
    _inside_root,
    _sign_executable,
    generate_sbom,
    verify_release,
)


ROOT = Path(__file__).resolve().parents[1]


def _file(path: Path, content: bytes = b"fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _valid_release(root: Path) -> Path:
    app = root / "LocalTavern"
    _file(app / "LocalTavern.exe", b"desktop-executable")
    _file(app / "_internal" / "web" / "index.html", b"<!doctype html>")
    _file(app / "_internal" / "prompts" / "system.md", "系统".encode())
    _file(app / "_internal" / "prompts" / "summary.md", "摘要".encode())
    _file(app / "_internal" / "PySide6" / "Qt" / "libexec" / "QtWebEngineProcess.exe")
    return app


def test_build_paths_must_be_strict_children_of_project(tmp_path):
    root = (tmp_path / "project").resolve()
    root.mkdir()
    child = root / "release"
    assert _inside_root(child, root) == child.resolve()

    for rejected in (root, root.parent, tmp_path / "outside"):
        with pytest.raises(DesktopBuildError, match="必须位于项目内"):
            _inside_root(rejected, root)

    escaping = root / "nested" / ".." / ".." / "outside"
    with pytest.raises(DesktopBuildError, match="必须位于项目内"):
        _inside_root(escaping, root)

    for protected in ("core", "data", "backups", ".venv-dev", "web/nested"):
        with pytest.raises(DesktopBuildError, match="受保护"):
            _inside_root(root / protected, root)


def test_build_rejects_overlapping_output_and_work_directories(monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(build_desktop, "ROOT", root)
    monkeypatch.setattr(
        build_desktop,
        "SPEC_PATH",
        root / "packaging" / "LocalTavern.spec",
    )
    with pytest.raises(DesktopBuildError, match="不能重叠"):
        build_desktop.build(
            output_dir=root / "release",
            work_dir=root / "release" / "work",
            clean=True,
        )


@pytest.mark.parametrize(
    "missing, message",
    [
        ("executable", "可执行文件不存在"),
        ("web", "缺少资源"),
        ("system", "缺少资源"),
        ("summary", "缺少资源"),
        ("qt_helper", "缺少 QtWebEngineProcess"),
    ],
)
def test_release_requires_executable_resources_and_qt_helper(
    tmp_path, missing, message
):
    app = _valid_release(tmp_path)
    targets = {
        "executable": app / "LocalTavern.exe",
        "web": app / "_internal" / "web" / "index.html",
        "system": app / "_internal" / "prompts" / "system.md",
        "summary": app / "_internal" / "prompts" / "summary.md",
        "qt_helper": app
        / "_internal"
        / "PySide6"
        / "Qt"
        / "libexec"
        / "QtWebEngineProcess.exe",
    }
    targets[missing].unlink()
    with pytest.raises(DesktopBuildError, match=message):
        verify_release(app)


@pytest.mark.parametrize(
    "relative",
    [
        "data/session.json",
        "_internal/backups/archive.zip",
        "logs/tavern.log",
        "_internal/provider-secrets.json",
    ],
)
def test_release_rejects_every_user_data_and_secret_location(tmp_path, relative):
    app = _valid_release(tmp_path)
    _file(app / relative, b"must-not-ship")
    with pytest.raises(DesktopBuildError, match="禁止打包的用户数据"):
        verify_release(app)


def test_release_manifest_is_sorted_complete_and_content_addressed(tmp_path):
    app = _valid_release(tmp_path)
    _file(app / "_internal" / "web" / "z-last.js", b"z")
    _file(app / "_internal" / "web" / "a-first.js", b"a")

    manifest = verify_release(app)
    files = manifest["files"]
    paths = [row["path"] for row in files]
    assert manifest["schema_version"] == 2
    assert manifest["application"] == "LocalTavern"
    assert manifest["packaging"] == "pyinstaller-onedir"
    assert manifest["file_count"] == len(files)
    assert manifest["total_bytes"] == sum(row["bytes"] for row in files)
    assert paths == sorted(paths)
    assert "LocalTavern.exe" in paths
    for row in files:
        target = app / row["path"]
        assert row["bytes"] == target.stat().st_size
        assert row["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest().upper()


def test_build_input_hash_and_sbom_are_deterministic_and_content_sensitive(tmp_path):
    root = tmp_path / "project"
    _file(root / "core" / "a.py", b"a")
    changed = _file(root / "web" / "index.html", b"before")
    _file(root / "requirements-desktop.lock.txt", b"Example-Package==1.2.3\n")
    _file(root / "package-lock.json", b'{"packages":{}}')
    app = _valid_release(tmp_path / "release")

    first_inputs = _build_input_manifest(root)
    second_inputs = _build_input_manifest(root)
    assert first_inputs == second_inputs
    changed.write_bytes(b"after")
    third_inputs = _build_input_manifest(root)
    assert third_inputs["aggregate_sha256"] != first_inputs["aggregate_sha256"]

    source = {"commit": "c" * 40, "dirty": False}
    signature = {"status": "unsigned"}
    first_sbom = generate_sbom(
        root=root,
        app_dir=app,
        build_inputs=third_inputs,
        source=source,
        signature=signature,
    )
    second_sbom = generate_sbom(
        root=root,
        app_dir=app,
        build_inputs=third_inputs,
        source=source,
        signature=signature,
    )
    assert first_sbom == second_sbom
    assert first_sbom["serialNumber"].startswith("urn:uuid:")


def test_build_checks_desktop_lock_runs_pyinstaller_and_writes_verified_manifest(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "project"
    root.mkdir()
    spec = _file(root / "packaging" / "LocalTavern.spec", b"# test spec")
    _file(root / "requirements-desktop.lock.txt", b"Example-Package==1.2.3\n")
    _file(root / "web" / "index.html", b"<!doctype html>")
    _file(
        root / "package-lock.json",
        json.dumps(
            {
                "packages": {
                    "": {"name": "fixture", "version": "0.0.0"},
                    "node_modules/example-node": {
                        "version": "4.5.6",
                        "license": "MIT",
                    },
                }
            }
        ).encode(),
    )
    monkeypatch.setattr(build_desktop, "ROOT", root)
    monkeypatch.setattr(build_desktop, "SPEC_PATH", spec)
    commands: list[list[str]] = []

    def run(command, **kwargs):
        assert kwargs["cwd"] == root.resolve()
        assert kwargs["check"] is False
        commands.append([str(item) for item in command])
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{'b' * 40}\n")
        if command[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(command, 0, stdout=" M web/index.html\n")
        if command[:2] == ["node", "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="v24.15.0\n")
        if command[:2] == ["npm", "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="11.12.1\n")
        if "PyInstaller" in command:
            distpath = Path(command[command.index("--distpath") + 1])
            _valid_release(distpath)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_desktop.subprocess, "run", run)
    output = root / "release"
    work = root / ".desktop-build"
    _file(output / "stale.txt")
    _file(work / "stale.txt")

    result = build_desktop.build(output_dir=output, work_dir=work, clean=True)

    assert commands[0][-3:] == [
        "tools/dependency_locks.py",
        "--environment",
        "desktop",
    ]
    pyinstaller_command = next(
        command for command in commands if "PyInstaller" in command
    )
    assert pyinstaller_command[:3] == [
        str(build_desktop.sys.executable),
        "-m",
        "PyInstaller",
    ]
    assert "--clean" in pyinstaller_command
    assert pyinstaller_command[-1] == str(spec)
    assert not (output / "stale.txt").exists()
    assert not (work / "stale.txt").exists()
    manifest_path = Path(result["manifest"])
    sbom_path = Path(result["sbom"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    assert result["status"] == "passed"
    assert result["signature_status"] == "unsigned"
    assert Path(result["executable"]).is_file()
    assert manifest["file_count"] == result["file_count"]
    assert manifest["total_bytes"] == result["total_bytes"]
    assert manifest["source"] == {
        "vcs": "git",
        "available": True,
        "commit": "b" * 40,
        "dirty": True,
    }
    assert manifest["toolchain"]["node"] == "v24.15.0"
    assert manifest["toolchain"]["npm"] == "11.12.1"
    assert manifest["build_inputs"]["file_count"] >= 4
    assert len(manifest["build_inputs"]["aggregate_sha256"]) == 64
    assert manifest["signature"] == {
        "status": "unsigned",
        "authenticode_verified": False,
        "reason": "certificate_not_provided",
    }
    assert (
        manifest["sbom"]["sha256"]
        == hashlib.sha256(sbom_path.read_bytes()).hexdigest().upper()
    )
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    component_names = {component["name"] for component in sbom["components"]}
    assert "Example-Package" in component_names
    assert "example-node" in component_names
    assert "web/index.html" in component_names


def test_build_stops_before_pyinstaller_when_desktop_lock_fails(monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(build_desktop, "ROOT", root)
    monkeypatch.setattr(
        build_desktop, "SPEC_PATH", root / "packaging" / "LocalTavern.spec"
    )
    commands = []

    def fail_lock(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(build_desktop.subprocess, "run", fail_lock)
    with pytest.raises(DesktopBuildError, match="环境与锁文件不一致"):
        build_desktop.build(
            output_dir=root / "release",
            work_dir=root / ".desktop-build",
            clean=False,
        )
    assert len(commands) == 1
    assert "PyInstaller" not in commands[0]


def test_build_aborts_if_source_inputs_change_during_pyinstaller(monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    spec = _file(root / "packaging" / "LocalTavern.spec")
    monkeypatch.setattr(build_desktop, "ROOT", root)
    monkeypatch.setattr(build_desktop, "SPEC_PATH", spec)
    monkeypatch.setattr(
        build_desktop,
        "_source_provenance",
        lambda _root: {"available": False, "commit": None, "dirty": None},
    )
    monkeypatch.setattr(build_desktop, "_toolchain_versions", lambda _root: {})
    input_manifests = iter(
        [
            {"aggregate_sha256": "A", "file_count": 1, "files": []},
            {"aggregate_sha256": "B", "file_count": 1, "files": []},
        ]
    )
    monkeypatch.setattr(
        build_desktop,
        "_build_input_manifest",
        lambda _root: next(input_manifests),
    )

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_desktop.subprocess, "run", run)
    with pytest.raises(DesktopBuildError, match="源输入发生变化"):
        build_desktop.build(
            output_dir=root / "release",
            work_dir=root / ".desktop-build",
            clean=False,
        )


def test_authenticode_pfx_hook_signs_then_verifies_without_manifest_secret(
    monkeypatch,
    tmp_path,
):
    executable = _file(tmp_path / "LocalTavern.exe", b"unsigned")
    certificate = _file(tmp_path / "release.pfx", b"certificate-bytes")
    signtool = _file(tmp_path / "signtool.exe", b"fixture")
    monkeypatch.setenv("TAVERN_SIGN_PASSWORD", "TOP-SECRET-PASSWORD")
    commands: list[list[str]] = []

    def run(command, **kwargs):
        assert kwargs == {"check": False, "text": True}
        commands.append([str(item) for item in command])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_desktop.subprocess, "run", run)
    signature = _sign_executable(
        executable,
        certificate=certificate,
        thumbprint=None,
        password_env="TAVERN_SIGN_PASSWORD",
        timestamp_url="https://timestamp.example.test",
        signtool=signtool,
        machine_store=False,
    )

    assert [command[1] for command in commands] == ["sign", "verify"]
    assert "/p" in commands[0]
    assert "TOP-SECRET-PASSWORD" in commands[0]
    assert signature == {
        "status": "signed",
        "authenticode_verified": True,
        "timestamped": True,
        "method": "pfx",
        "certificate_sha256": hashlib.sha256(certificate.read_bytes())
        .hexdigest()
        .upper(),
    }
    assert "TOP-SECRET-PASSWORD" not in json.dumps(signature)
    assert str(certificate) not in json.dumps(signature)


@pytest.mark.parametrize(
    ("returncodes", "message"),
    [
        ([9], "签名失败"),
        ([0, 7], "签名验证失败"),
    ],
)
def test_authenticode_requested_failure_aborts(
    monkeypatch,
    tmp_path,
    returncodes,
    message,
):
    executable = _file(tmp_path / "LocalTavern.exe")
    certificate = _file(tmp_path / "release.pfx")
    signtool = _file(tmp_path / "signtool.exe")
    outcomes = iter(returncodes)

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, next(outcomes))

    monkeypatch.setattr(build_desktop.subprocess, "run", run)
    with pytest.raises(DesktopBuildError, match=message):
        _sign_executable(
            executable,
            certificate=certificate,
            thumbprint=None,
            password_env=None,
            timestamp_url=None,
            signtool=signtool,
            machine_store=False,
        )


def test_authenticode_absence_is_explicit_and_does_not_claim_signature():
    signature = _sign_executable(
        Path("LocalTavern.exe"),
        certificate=None,
        thumbprint=None,
        password_env=None,
        timestamp_url=None,
        signtool=None,
        machine_store=False,
    )
    assert signature["status"] == "unsigned"
    assert signature["authenticode_verified"] is False


def test_authenticode_rejects_ambiguous_or_insecure_options(tmp_path):
    executable = _file(tmp_path / "LocalTavern.exe")
    certificate = _file(tmp_path / "release.pfx")
    with pytest.raises(DesktopBuildError, match="不能同时"):
        _sign_executable(
            executable,
            certificate=certificate,
            thumbprint="a" * 40,
            password_env=None,
            timestamp_url=None,
            signtool=None,
            machine_store=False,
        )
    with pytest.raises(DesktopBuildError, match="必须使用 HTTPS"):
        _sign_executable(
            executable,
            certificate=certificate,
            thumbprint=None,
            password_env=None,
            timestamp_url="http://insecure.example.test",
            signtool=None,
            machine_store=False,
        )


def test_pyinstaller_spec_packages_only_static_resources_and_excludes_server_stack():
    source = (ROOT / "packaging" / "LocalTavern.spec").read_text(encoding="utf-8")
    assert '[str(ROOT / "desktop" / "__main__.py")]' in source
    assert '(str(ROOT / "web"), "web")' in source
    assert '(str(ROOT / "prompts"), "prompts")' in source
    for forbidden_data in ("data", "backups", "logs", "provider-secrets.json"):
        assert f'ROOT / "{forbidden_data}"' not in source
    for excluded in ("uvicorn", "httptools", "watchfiles", "websockets"):
        assert f'"{excluded}"' in source


def test_pyinstaller_spec_resolves_project_root_from_packaging_specpath():
    spec_path = ROOT / "packaging" / "LocalTavern.spec"
    source = spec_path.read_text(encoding="utf-8")
    captured: dict[str, object] = {}

    def analysis(scripts, **kwargs):
        captured["scripts"] = scripts
        captured["analysis"] = kwargs
        return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])

    namespace = {
        "SPECPATH": str(spec_path.parent),
        "Analysis": analysis,
        "PYZ": lambda *_args, **_kwargs: object(),
        "EXE": lambda *_args, **_kwargs: object(),
        "COLLECT": lambda *_args, **_kwargs: object(),
    }

    exec(compile(source, str(spec_path), "exec"), namespace)

    assert namespace["ROOT"] == ROOT.resolve()
    assert captured["scripts"] == [str(ROOT / "desktop" / "__main__.py")]
    analysis_options = captured["analysis"]
    assert analysis_options["pathex"] == [str(ROOT)]
    assert analysis_options["datas"] == [
        (str(ROOT / "web"), "web"),
        (str(ROOT / "prompts"), "prompts"),
    ]
