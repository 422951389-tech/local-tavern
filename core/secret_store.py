"""Windows 当前用户 DPAPI Provider 密钥存储。"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import tempfile
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

from core.library_lock import library_lock


_SECRET_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_DPAPI_ENTROPY = b"LocalTavern.ProviderCredential.v1"


class SecretStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def as_detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class SecretProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, protected: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(
        len(value),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


class DPAPIProtector:
    """使用 CryptProtectData/CryptUnprotectData 绑定当前 Windows 用户。"""

    _UI_FORBIDDEN = 0x01

    def __init__(self) -> None:
        if os.name != "nt":
            raise SecretStoreError(
                "dpapi_unavailable",
                "当前系统不支持 Windows DPAPI",
            )
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    def protect(self, plaintext: bytes) -> bytes:
        source, source_buffer = _blob(plaintext)
        entropy, entropy_buffer = _blob(_DPAPI_ENTROPY)
        output = _DataBlob()
        _ = source_buffer, entropy_buffer
        if not self._crypt32.CryptProtectData(
            ctypes.byref(source),
            "Local Tavern provider credential",
            ctypes.byref(entropy),
            None,
            None,
            self._UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            raise SecretStoreError("secret_protect_failed", "Provider 凭据加密失败")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))

    def unprotect(self, protected: bytes) -> bytes:
        source, source_buffer = _blob(protected)
        entropy, entropy_buffer = _blob(_DPAPI_ENTROPY)
        output = _DataBlob()
        description = wintypes.LPWSTR()
        _ = source_buffer, entropy_buffer
        if not self._crypt32.CryptUnprotectData(
            ctypes.byref(source),
            ctypes.byref(description),
            ctypes.byref(entropy),
            None,
            None,
            self._UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            raise SecretStoreError("secret_unprotect_failed", "Provider 凭据解密失败")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))
            if description:
                self._kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))


def default_app_data_dir() -> Path:
    override = os.environ.get("TAVERN_PROVIDER_DATA_DIR")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise SecretStoreError(
                "provider_storage_path_invalid",
                "Provider 存储路径必须是绝对路径",
            )
        return path.resolve(strict=False)
    raw = os.environ.get("LOCALAPPDATA")
    if not raw:
        if os.name != "nt":
            raise SecretStoreError(
                "local_app_data_unavailable",
                "无法确定本机应用数据目录",
            )
        raw = str(Path.home() / "AppData" / "Local")
    return Path(raw).expanduser().resolve(strict=False) / "LocalTavern"


def _path_override(name: str, fallback: Path) -> Path:
    raw = os.environ.get(name)
    if not raw:
        return fallback
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SecretStoreError(
            "provider_storage_path_invalid",
            "Provider 存储路径必须是绝对路径",
        )
    return path.resolve(strict=False)


def default_provider_config_path() -> Path:
    return _path_override(
        "TAVERN_PROVIDER_CONFIG_PATH",
        default_app_data_dir() / "providers.json",
    )


def default_provider_secrets_path() -> Path:
    return _path_override(
        "TAVERN_PROVIDER_SECRETS_PATH",
        default_app_data_dir() / "provider-secrets.json",
    )


def atomic_write_json(path: Path, payload: object) -> None:
    """同目录临时文件、fsync、原子替换；失败不保留临时文件。"""

    with library_lock.shared_write():
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            text=True,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            try:
                target.chmod(0o600)
            except OSError:
                pass
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def _validate_secret_id(secret_id: object) -> str:
    if not isinstance(secret_id, str) or _SECRET_ID_RE.fullmatch(secret_id) is None:
        raise SecretStoreError("secret_id_invalid", "Provider 凭据标识无效")
    return secret_id


class SecretStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        protector: SecretProtector | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_provider_secrets_path()
        self._protector = protector or DPAPIProtector()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise SecretStoreError(
                "secret_store_invalid", "Provider 凭据存储损坏"
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise SecretStoreError("secret_store_invalid", "Provider 凭据存储损坏")
        secrets = payload.get("secrets")
        if not isinstance(secrets, dict) or any(
            not isinstance(key, str)
            or _SECRET_ID_RE.fullmatch(key) is None
            or not isinstance(value, str)
            for key, value in secrets.items()
        ):
            raise SecretStoreError("secret_store_invalid", "Provider 凭据存储损坏")
        return dict(secrets)

    def _write(self, secrets: dict[str, str]) -> None:
        try:
            atomic_write_json(
                self.path,
                {"schema_version": 1, "secrets": dict(sorted(secrets.items()))},
            )
        except OSError as exc:
            raise SecretStoreError(
                "secret_store_write_failed", "Provider 凭据保存失败"
            ) from exc

    def configured(self, secret_id: str) -> bool:
        key = _validate_secret_id(secret_id)
        with self._lock:
            return key in self._read()

    def list_keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._read()))

    def get(self, secret_id: str) -> str | None:
        key = _validate_secret_id(secret_id)
        with self._lock:
            encoded = self._read().get(key)
            if encoded is None:
                return None
            try:
                protected = base64.b64decode(encoded, validate=True)
                plaintext = self._protector.unprotect(protected)
                if not isinstance(plaintext, bytes):
                    raise ValueError("invalid protected value")
                return plaintext.decode("utf-8")
            except SecretStoreError:
                raise
            except (UnicodeError, ValueError) as exc:
                raise SecretStoreError(
                    "secret_store_invalid",
                    "Provider 凭据存储损坏",
                ) from exc

    def set(self, secret_id: str, secret: str) -> None:
        key = _validate_secret_id(secret_id)
        if (
            not isinstance(secret, str)
            or not secret
            or secret != secret.strip()
            or len(secret) > 16_384
        ):
            raise SecretStoreError("secret_value_invalid", "Provider 凭据无效")
        try:
            protected = self._protector.protect(secret.encode("utf-8"))
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(
                "secret_protect_failed", "Provider 凭据加密失败"
            ) from exc
        if not isinstance(protected, bytes) or not protected:
            raise SecretStoreError("secret_protect_failed", "Provider 凭据加密失败")
        encoded = base64.b64encode(protected).decode("ascii")
        with self._lock:
            secrets = self._read()
            secrets[key] = encoded
            self._write(secrets)

    def delete(self, secret_id: str) -> bool:
        key = _validate_secret_id(secret_id)
        with self._lock:
            secrets = self._read()
            removed = secrets.pop(key, None) is not None
            if removed:
                self._write(secrets)
            return removed
