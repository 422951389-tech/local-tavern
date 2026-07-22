"""Validate the exact Node development manifest, npm lock, and installed tree."""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_JSON = ROOT / "package.json"
PACKAGE_LOCK = ROOT / "package-lock.json"
EXPECTED_NODE = "24.15.0"
EXPECTED_NPM = "11.12.1"
EXPECTED_DIRECT = frozenset({"@axe-core/playwright", "playwright"})
EXPECTED_CLOSURE = frozenset({
    "@axe-core/playwright",
    "axe-core",
    "playwright",
    "playwright-core",
})
_EXACT_VERSION = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_INTEGRITY = re.compile(r"^sha512-[A-Za-z0-9+/]+={0,2}$")


class NodeLockValidationError(ValueError):
    """The Node manifest or lock cannot prove an exact registry artifact set."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise NodeLockValidationError(f"JSON contains duplicate key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except NodeLockValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NodeLockValidationError(f"Node dependency file is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise NodeLockValidationError(f"{path.name} must contain a JSON object")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise NodeLockValidationError(f"{label} must be an object")
    return value


def _exact_dependencies(value: object, label: str) -> dict[str, str]:
    dependencies = _mapping(value, label)
    names = list(dependencies)
    if names != sorted(names, key=str.casefold):
        raise NodeLockValidationError(f"{label} must be sorted by package name")
    result: dict[str, str] = {}
    for name, version in dependencies.items():
        if not isinstance(name, str) or not name or not isinstance(version, str):
            raise NodeLockValidationError(f"{label} contains an invalid package entry")
        if _EXACT_VERSION.fullmatch(version) is None:
            raise NodeLockValidationError(f"{label} must pin an exact version: {name}")
        result[name] = version
    return result


def validate_manifest(manifest: Mapping[str, object]) -> dict[str, str]:
    if manifest.get("name") != "local-tavern-dev-tools":
        raise NodeLockValidationError("package.json name is not the project development manifest")
    if manifest.get("private") is not True:
        raise NodeLockValidationError("package.json must be private")
    if manifest.get("version") != "0.0.0":
        raise NodeLockValidationError("package.json version must remain 0.0.0")
    dependencies = manifest.get("dependencies", {})
    if dependencies != {}:
        raise NodeLockValidationError("production dependencies are forbidden")
    engines = _mapping(manifest.get("engines"), "engines")
    if engines.get("node") != EXPECTED_NODE or engines.get("npm") != EXPECTED_NPM:
        raise NodeLockValidationError("Node/npm engines do not match .node-version policy")
    if manifest.get("packageManager") != f"npm@{EXPECTED_NPM}":
        raise NodeLockValidationError("packageManager must pin the approved npm version")
    dev = _exact_dependencies(manifest.get("devDependencies"), "devDependencies")
    if set(dev) != EXPECTED_DIRECT:
        raise NodeLockValidationError(
            "devDependencies must contain only the approved browser gate packages"
        )
    return dev


def _package_name(lock_path: str) -> str:
    prefix = "node_modules/"
    if not lock_path.startswith(prefix):
        raise NodeLockValidationError(f"lock contains a non-registry package path: {lock_path}")
    return lock_path[len(prefix):]


def _validate_registry_artifact(name: str, record: Mapping[str, object]) -> str:
    version = record.get("version")
    if not isinstance(version, str) or _EXACT_VERSION.fullmatch(version) is None:
        raise NodeLockValidationError(f"lock package has no exact version: {name}")
    resolved = record.get("resolved")
    if not isinstance(resolved, str):
        raise NodeLockValidationError(f"lock package has no resolved artifact: {name}")
    parsed = urlsplit(resolved)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "registry.npmjs.org"
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise NodeLockValidationError(f"lock package is not from the public npm registry: {name}")
    integrity = record.get("integrity")
    if not isinstance(integrity, str) or _INTEGRITY.fullmatch(integrity) is None:
        raise NodeLockValidationError(f"lock package has no SHA-512 integrity: {name}")
    try:
        digest = base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise NodeLockValidationError(
            f"lock package has invalid SHA-512 integrity: {name}"
        ) from exc
    if len(digest) != 64:
        raise NodeLockValidationError(f"lock package has invalid SHA-512 integrity: {name}")
    if record.get("dev") is not True:
        raise NodeLockValidationError(f"browser gate package is not marked dev-only: {name}")
    return version


def validate_lock(lock: Mapping[str, object], direct: Mapping[str, str]) -> dict[str, str]:
    if lock.get("name") != "local-tavern-dev-tools" or lock.get("version") != "0.0.0":
        raise NodeLockValidationError("package-lock identity differs from package.json")
    if lock.get("lockfileVersion") != 3 or lock.get("requires") is not True:
        raise NodeLockValidationError("package-lock must use lockfileVersion 3 with requires=true")
    packages = _mapping(lock.get("packages"), "package-lock packages")
    root = _mapping(packages.get(""), "package-lock root package")
    if root.get("name") != "local-tavern-dev-tools" or root.get("version") != "0.0.0":
        raise NodeLockValidationError("package-lock root identity differs from package.json")
    if root.get("dependencies", {}) != {}:
        raise NodeLockValidationError("package-lock contains production dependencies")
    locked_direct = _exact_dependencies(root.get("devDependencies"), "locked devDependencies")
    if locked_direct != dict(direct):
        raise NodeLockValidationError("package.json and package-lock direct dependencies differ")

    locked: dict[str, str] = {}
    for lock_path, raw_record in packages.items():
        if lock_path == "":
            continue
        if not isinstance(lock_path, str):
            raise NodeLockValidationError("package-lock contains a non-string package path")
        name = _package_name(lock_path)
        if name in locked:
            raise NodeLockValidationError(f"package-lock contains duplicate package identity: {name}")
        locked[name] = _validate_registry_artifact(
            name,
            _mapping(raw_record, f"package-lock record {name}"),
        )
    if set(locked) != EXPECTED_CLOSURE:
        missing = sorted(EXPECTED_CLOSURE - set(locked))
        extra = sorted(set(locked) - EXPECTED_CLOSURE)
        raise NodeLockValidationError(
            f"package-lock closure differs from the approved set (missing={missing}, extra={extra})"
        )
    for name, version in direct.items():
        if locked.get(name) != version:
            raise NodeLockValidationError(f"direct lock version differs for {name}")
    if locked.get("playwright-core") != locked.get("playwright"):
        raise NodeLockValidationError("playwright and playwright-core versions must match")
    return locked


def validate_installed_tree(tree: Mapping[str, object], direct: Mapping[str, str]) -> dict[str, str]:
    dependencies = _mapping(tree.get("dependencies"), "npm installed dependencies")
    if set(dependencies) != set(direct):
        missing = sorted(set(direct) - set(dependencies))
        extra = sorted(set(dependencies) - set(direct))
        raise NodeLockValidationError(
            f"installed Node package set differs (missing={missing}, extra={extra})"
        )
    installed: dict[str, str] = {}
    for name, raw_record in dependencies.items():
        record = _mapping(raw_record, f"installed package {name}")
        if any(record.get(flag) for flag in ("extraneous", "invalid", "missing")):
            raise NodeLockValidationError(f"installed package is not clean: {name}")
        version = record.get("version")
        if version != direct[name]:
            raise NodeLockValidationError(f"installed package version differs for {name}")
        installed[name] = version
    return installed


def _run_version(command: str, argument: str, label: str) -> str:
    completed = subprocess.run(
        [command, argument],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise NodeLockValidationError(f"cannot query {label} version")
    return completed.stdout.strip().removeprefix("v")


def verify_environment(root: Path, direct: Mapping[str, str], node: str, npm: str) -> dict:
    node_version = _run_version(node, "--version", "Node")
    npm_version = _run_version(npm, "--version", "npm")
    if node_version != EXPECTED_NODE or npm_version != EXPECTED_NPM:
        raise NodeLockValidationError(
            f"Node/npm environment differs (node={node_version}, npm={npm_version})"
        )
    completed = subprocess.run(
        [npm, "ls", "--depth=0", "--json"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    try:
        tree = json.loads(completed.stdout, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, NodeLockValidationError) as exc:
        raise NodeLockValidationError("npm ls did not return valid JSON") from exc
    if completed.returncode != 0 or not isinstance(tree, dict):
        raise NodeLockValidationError("npm ls reports an invalid installed tree")
    installed = validate_installed_tree(tree, direct)
    return {
        "node": node_version,
        "npm": npm_version,
        "direct_packages": installed,
    }


def _resolve_executable(value: str, label: str) -> str:
    candidate = Path(value)
    if candidate.is_file():
        return str(candidate.resolve())
    found = shutil.which(value)
    if found:
        return found
    raise NodeLockValidationError(f"cannot find {label}: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate exact Node development dependencies")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--semantic-only", action="store_true")
    parser.add_argument("--environment", action="store_true")
    parser.add_argument("--node", default="node")
    parser.add_argument("--npm", default="npm")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        direct = validate_manifest(read_json(root / PACKAGE_JSON.name))
        locked = None
        environment = None
        if not args.semantic_only:
            locked = validate_lock(read_json(root / PACKAGE_LOCK.name), direct)
        if args.environment:
            if args.semantic_only:
                raise NodeLockValidationError("environment validation requires package-lock.json")
            environment = verify_environment(
                root,
                direct,
                _resolve_executable(args.node, "Node"),
                _resolve_executable(args.npm, "npm"),
            )
    except NodeLockValidationError as exc:
        print(f"[Node dependency lock failed] {exc}")
        return 1
    print(json.dumps({
        "ok": True,
        "direct_packages": direct,
        "locked_packages": 0 if locked is None else len(locked),
        "integrity_checked": not args.semantic_only,
        "environment": environment,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
