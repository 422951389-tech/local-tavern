"""Provider 非秘密配置、实例解析与凭据协调。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx

from core.async_utils import acquire_critical, await_critical
from core.config import OLLAMA_HOST, PROMPT_CONTEXT_FALLBACK
from core.library_lock import library_lock
from core.model_provider import (
    DNSResolver,
    ModelProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderValidationError,
    validate_cloud_base_url,
)
from core.providers import AnthropicProvider, OllamaProvider, OpenAICompatibleProvider
from core.secret_store import (
    SecretStore,
    atomic_write_json,
    default_provider_config_path,
    default_provider_secrets_path,
)


PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PROVIDER_KINDS = frozenset({"ollama", "openai_compatible", "anthropic"})
DEFAULT_CLOUD_CONTEXT_LIMIT = 32_768

logger = logging.getLogger(__name__)

PROVIDER_PRESETS: dict[str, dict[str, object]] = {
    "openai": {
        "id": "openai",
        "name": "OpenAI",
        "kind": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "context_limit": DEFAULT_CLOUD_CONTEXT_LIMIT,
    },
    "deepseek": {
        "id": "deepseek",
        "name": "DeepSeek",
        "kind": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "context_limit": DEFAULT_CLOUD_CONTEXT_LIMIT,
    },
    "siliconflow": {
        "id": "siliconflow",
        "name": "SiliconFlow",
        "kind": "openai_compatible",
        "base_url": "https://api.siliconflow.cn/v1",
        "context_limit": DEFAULT_CLOUD_CONTEXT_LIMIT,
    },
    "anthropic": {
        "id": "anthropic",
        "name": "Anthropic",
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com",
        "context_limit": DEFAULT_CLOUD_CONTEXT_LIMIT,
    },
    "custom": {
        "id": "custom",
        "name": "自定义",
        "kind": "openai_compatible",
        "base_url": None,
        "context_limit": DEFAULT_CLOUD_CONTEXT_LIMIT,
    },
}


class ProviderRegistryError(ProviderError):
    pass


@dataclass(eq=False)
class _ProviderSlot:
    provider_id: str
    version: int
    config: ProviderConfig
    provider: ModelProvider
    leases: int = 0
    retired: bool = False
    close_started: bool = False


class ProviderLease:
    """Provider 实例的显式生命周期租约。"""

    def __init__(self, registry: ProviderRegistry, slot: _ProviderSlot) -> None:
        self._registry = registry
        self._slot = slot
        self._released = False

    @property
    def provider(self) -> ModelProvider:
        return self._slot.provider

    @property
    def provider_id(self) -> str:
        return self._slot.provider_id

    @property
    def config(self) -> ProviderConfig:
        """该租约绑定版本的不可变配置快照。"""
        return self._slot.config

    @property
    def version(self) -> int:
        return self._slot.version

    def retain(self) -> ProviderLease:
        """为同一实例版本增加独立租约；已释放租约不能复用。"""
        if self._released:
            raise RuntimeError("Provider 租约已释放")
        return self._registry._retain_lease(self._slot)

    async def __aenter__(self) -> ModelProvider:
        if self._released:
            raise RuntimeError("Provider 租约已释放")
        return self._slot.provider

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.release()

    async def release(self) -> None:
        """幂等释放；retired 实例的最后一个租约负责触发关闭。"""
        if self._released:
            return
        self._released = True
        await await_critical(self._registry._release_lease(self._slot))


def _validate_provider_id(value: object) -> str:
    if not isinstance(value, str) or PROVIDER_ID_RE.fullmatch(value) is None:
        raise ProviderValidationError("provider_id_invalid", "Provider ID 无效")
    return value


def _normalize_models(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > 100:
        raise ProviderValidationError(
            "provider_models_invalid", "Provider 模型列表无效"
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or len(item) > 200
            or any(ord(character) < 32 or ord(character) == 127 for character in item)
        ):
            raise ProviderValidationError(
                "provider_models_invalid", "Provider 模型列表无效"
            )
        if item not in seen:
            seen.add(item)
            normalized.append(item)
    return tuple(normalized)


def _normalize_context_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 4_096 <= value <= 1_048_576
    ):
        raise ProviderValidationError(
            "provider_context_limit_invalid",
            "Provider 上下文窗口无效",
        )
    return value


def capabilities_for_kind(kind: str) -> ProviderCapabilities:
    if kind == "ollama":
        return OllamaProvider.capabilities
    if kind == "anthropic":
        return AnthropicProvider.capabilities
    return OpenAICompatibleProvider.capabilities


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    kind: str
    name: str
    base_url: str
    context_limit: int
    models: tuple[str, ...] = ()
    preset: str = "custom"

    def as_json(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "name": self.name,
            "base_url": self.base_url,
            "context_limit": self.context_limit,
            "models": list(self.models),
            "preset": self.preset,
        }

    def as_public(self, *, has_credential: bool) -> dict[str, object]:
        return {
            **self.as_json(),
            "credential_required": self.kind != "ollama",
            "has_credential": has_credential,
            "capabilities": capabilities_for_kind(self.kind).as_public(),
        }


def build_provider_config(
    provider_id: object,
    *,
    kind: object,
    name: object,
    base_url: object,
    models: object = None,
    context_limit: object = DEFAULT_CLOUD_CONTEXT_LIMIT,
    preset: object = "custom",
    resolver: DNSResolver | None = None,
    resolve_dns: bool = True,
) -> ProviderConfig:
    normalized_id = _validate_provider_id(provider_id)
    if not isinstance(kind, str) or kind not in PROVIDER_KINDS - {"ollama"}:
        raise ProviderValidationError("provider_kind_invalid", "Provider 类型无效")
    if (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or len(name) > 80
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise ProviderValidationError("provider_name_invalid", "Provider 名称无效")
    if not isinstance(preset, str) or preset not in PROVIDER_PRESETS:
        raise ProviderValidationError("provider_preset_invalid", "Provider 预设无效")
    normalized_url = validate_cloud_base_url(
        base_url,
        resolver=resolver,
        resolve_dns=resolve_dns,
    )
    return ProviderConfig(
        provider_id=normalized_id,
        kind=kind,
        name=name,
        base_url=normalized_url,
        context_limit=_normalize_context_limit(context_limit),
        models=_normalize_models(models),
        preset=preset,
    )


class ProviderRegistry:
    def __init__(
        self,
        config_path: Path | None = None,
        *,
        secret_store: SecretStore | None = None,
        resolver: DNSResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config_path = (
            Path(config_path)
            if config_path is not None
            else default_provider_config_path()
        )
        default_secret_path = (
            self.config_path.parent / "provider-secrets.json"
            if config_path is not None
            else default_provider_secrets_path()
        )
        self.secret_store = secret_store or SecretStore(default_secret_path)
        self._resolver = resolver
        self._transport = transport
        self._lock = threading.RLock()
        self._configs = self._load()
        self._instances: dict[str, _ProviderSlot] = {}
        self._retired_instances: set[_ProviderSlot] = set()
        self._versions: dict[str, int] = {}
        self._closed = False

    @staticmethod
    def presets() -> list[dict[str, object]]:
        return [dict(PROVIDER_PRESETS[key]) for key in PROVIDER_PRESETS]

    @staticmethod
    def _ollama_config() -> ProviderConfig:
        return ProviderConfig(
            provider_id="ollama",
            kind="ollama",
            name="Ollama",
            base_url=OLLAMA_HOST,
            context_limit=PROMPT_CONTEXT_FALLBACK,
            preset="custom",
        )

    def _load(self) -> dict[str, ProviderConfig]:
        if not self.config_path.exists():
            return {}
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ProviderRegistryError(
                "provider_config_store_invalid",
                "Provider 配置存储损坏",
            ) from exc
        rows = payload.get("providers") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(rows, list)
        ):
            raise ProviderRegistryError(
                "provider_config_store_invalid",
                "Provider 配置存储损坏",
            )
        configs: dict[str, ProviderConfig] = {}
        try:
            for row in rows:
                if not isinstance(row, dict):
                    raise ProviderValidationError(
                        "provider_config_invalid", "Provider 配置无效"
                    )
                config = build_provider_config(
                    row.get("provider_id"),
                    kind=row.get("kind"),
                    name=row.get("name"),
                    base_url=row.get("base_url"),
                    models=row.get("models"),
                    context_limit=row.get("context_limit", DEFAULT_CLOUD_CONTEXT_LIMIT),
                    preset=row.get("preset", "custom"),
                    resolver=self._resolver,
                    # 离线启动不能因已保存的云端域名暂时无法解析而失败；
                    # 真正发请求前 HTTPProviderBase 会重新解析并阻断非公网地址。
                    resolve_dns=False,
                )
                if config.provider_id in configs or config.provider_id == "ollama":
                    raise ProviderValidationError(
                        "provider_config_invalid", "Provider 配置无效"
                    )
                configs[config.provider_id] = config
        except ProviderError as exc:
            raise ProviderRegistryError(
                "provider_config_store_invalid",
                "Provider 配置存储损坏",
            ) from exc
        return configs

    def _persist(self) -> None:
        try:
            atomic_write_json(
                self.config_path,
                {
                    "schema_version": 1,
                    "providers": [
                        self._configs[key].as_json() for key in sorted(self._configs)
                    ],
                },
            )
        except OSError as exc:
            raise ProviderRegistryError(
                "provider_config_write_failed",
                "Provider 配置保存失败",
            ) from exc

    def list_configs(self) -> list[dict[str, object]]:
        with self._lock:
            configs = [
                self._ollama_config(),
                *(self._configs[key] for key in sorted(self._configs)),
            ]
            return [
                config.as_public(
                    has_credential=(
                        True
                        if config.kind == "ollama"
                        else self.secret_store.configured(config.provider_id)
                    )
                )
                for config in configs
            ]

    def get_config(self, provider_id: str) -> ProviderConfig:
        key = _validate_provider_id(provider_id)
        if key == "ollama":
            return self._ollama_config()
        with self._lock:
            config = self._configs.get(key)
        if config is None:
            raise ProviderRegistryError("provider_not_found", "Provider 不存在")
        return config

    def public_config(self, provider_id: str) -> dict[str, object]:
        config = self.get_config(provider_id)
        return config.as_public(
            has_credential=(
                True
                if config.kind == "ollama"
                else self.secret_store.configured(config.provider_id)
            )
        )

    def build_config(
        self,
        provider_id: object,
        *,
        kind: object,
        name: object,
        base_url: object,
        models: object = None,
        context_limit: object = DEFAULT_CLOUD_CONTEXT_LIMIT,
        preset: object = "custom",
    ) -> ProviderConfig:
        return build_provider_config(
            provider_id,
            kind=kind,
            name=name,
            base_url=base_url,
            models=models,
            context_limit=context_limit,
            preset=preset,
            resolver=self._resolver,
        )

    def _build_instance(self, config: ProviderConfig) -> ModelProvider:
        if config.kind == "ollama":
            return OllamaProvider()

        credential = self.secret_store.get(config.provider_id)
        if credential is None:
            raise ProviderRegistryError(
                "provider_credential_missing",
                "Provider 凭据未配置",
            )
        if config.kind == "anthropic":
            return AnthropicProvider(
                config.base_url,
                credential,
                configured_models=config.models,
                context_limit=config.context_limit,
                resolver=self._resolver,
                transport=self._transport,
            )
        return OpenAICompatibleProvider(
            config.base_url,
            credential,
            configured_models=config.models,
            context_limit=config.context_limit,
            resolver=self._resolver,
            transport=self._transport,
        )

    def _get_or_create_slot_locked(self, config: ProviderConfig) -> _ProviderSlot:
        if self._closed:
            raise ProviderRegistryError(
                "provider_registry_closed",
                "Provider 注册表已关闭",
            )
        slot = self._instances.get(config.provider_id)
        if slot is None:
            slot = _ProviderSlot(
                provider_id=config.provider_id,
                version=self._versions.get(config.provider_id, 0),
                config=config,
                provider=self._build_instance(config),
            )
            self._instances[config.provider_id] = slot
        return slot

    def _retain_lease(self, slot: _ProviderSlot) -> ProviderLease:
        with self._lock:
            if self._closed or slot.close_started:
                raise ProviderRegistryError(
                    "provider_lease_unavailable",
                    "Provider 租约已失效",
                )
            slot.leases += 1
        return ProviderLease(self, slot)

    def _retire_instance_locked(self, provider_id: str) -> list[_ProviderSlot]:
        """推进版本并退役当前实例，返回可立即关闭的零租约实例。"""
        self._versions[provider_id] = self._versions.get(provider_id, 0) + 1
        slot = self._instances.pop(provider_id, None)
        if slot is None:
            return []
        slot.retired = True
        if slot.leases:
            self._retired_instances.add(slot)
            return []
        slot.close_started = True
        return [slot]

    async def _close_slots(self, slots: list[_ProviderSlot]) -> None:
        for slot in slots:
            try:
                await slot.provider.close()
            except Exception as exc:  # noqa: BLE001 - close 是 best-effort 清理边界
                logger.warning(
                    "Provider instance close failed: provider_id=%s error_type=%s",
                    slot.provider_id,
                    type(exc).__name__,
                )

    async def _run_sync_update(self, callback, close_slots: list[_ProviderSlot]):
        """状态写线程与实例清理作为同一不可分割的异步临界区收口。"""

        async def operation():
            try:
                return await asyncio.to_thread(callback)
            finally:
                await self._close_slots(close_slots)

        return await await_critical(operation())

    async def _release_lease(self, slot: _ProviderSlot) -> None:
        close_slots: list[_ProviderSlot] = []
        with self._lock:
            if slot.leases <= 0:
                return
            slot.leases -= 1
            if slot.retired and slot.leases == 0 and not slot.close_started:
                slot.close_started = True
                self._retired_instances.discard(slot)
                close_slots.append(slot)
        await await_critical(self._close_slots(close_slots))

    async def upsert_config(
        self,
        config: ProviderConfig,
        credential: str | None = None,
    ) -> dict[str, object]:
        if config.provider_id == "ollama" or config.kind == "ollama":
            raise ProviderRegistryError(
                "provider_builtin_immutable", "内置 Provider 不可修改"
            )
        validated = await asyncio.to_thread(
            build_provider_config,
            config.provider_id,
            kind=config.kind,
            name=config.name,
            base_url=config.base_url,
            models=config.models,
            context_limit=config.context_limit,
            preset=config.preset,
            resolver=self._resolver,
        )
        close_slots: list[_ProviderSlot] = []

        def upsert_sync() -> dict[str, object]:
            with library_lock.shared_write(), self._lock:
                before = self._configs.get(validated.provider_id)
                if (
                    before is not None
                    and (before.kind, before.base_url)
                    != (validated.kind, validated.base_url)
                    and self.secret_store.configured(validated.provider_id)
                ):
                    raise ProviderRegistryError(
                        "provider_credential_rebind_required",
                        "修改接口类型或服务地址前，请先删除已保存的 Provider 密钥",
                    )
                self._configs[validated.provider_id] = validated
                try:
                    self._persist()
                except BaseException:
                    if before is None:
                        self._configs.pop(validated.provider_id, None)
                    else:
                        self._configs[validated.provider_id] = before
                    raise
                try:
                    if credential is not None:
                        self.secret_store.set(validated.provider_id, credential)
                finally:
                    close_slots.extend(
                        self._retire_instance_locked(validated.provider_id)
                    )
                return self.public_config(validated.provider_id)

        return await self._run_sync_update(upsert_sync, close_slots)

    async def delete_config(self, provider_id: str) -> bool:
        key = _validate_provider_id(provider_id)
        if key == "ollama":
            raise ProviderRegistryError(
                "provider_builtin_immutable", "内置 Provider 不可删除"
            )
        close_slots: list[_ProviderSlot] = []

        def delete_sync() -> bool:
            with library_lock.shared_write(), self._lock:
                existing = self._configs.get(key)
                if existing is None:
                    raise ProviderRegistryError("provider_not_found", "Provider 不存在")

                # 两个独立文件无法组成一次原子提交。先销毁凭据，确保配置落盘失败时
                # 最坏结果只是保留一条“待重新配置密钥”的可见配置，而不是留下同 ID
                # 可复用的孤立密钥。
                self.secret_store.delete(key)
                self._configs.pop(key)
                try:
                    self._persist()
                except BaseException:
                    self._configs[key] = existing
                    raise
                finally:
                    close_slots.extend(self._retire_instance_locked(key))
                return True

        return await self._run_sync_update(delete_sync, close_slots)

    async def set_credential(
        self, provider_id: str, credential: str
    ) -> dict[str, object]:
        close_slots: list[_ProviderSlot] = []

        def set_sync() -> dict[str, object]:
            with library_lock.shared_write(), self._lock:
                config = self.get_config(provider_id)
                if config.kind == "ollama":
                    raise ProviderRegistryError(
                        "provider_credential_not_required",
                        "内置 Provider 无需凭据",
                    )
                self.secret_store.set(config.provider_id, credential)
                close_slots.extend(self._retire_instance_locked(config.provider_id))
                return self.public_config(config.provider_id)

        return await self._run_sync_update(set_sync, close_slots)

    async def delete_credential(self, provider_id: str) -> bool:
        close_slots: list[_ProviderSlot] = []

        def delete_sync() -> bool:
            with library_lock.shared_write(), self._lock:
                config = self.get_config(provider_id)
                if config.kind == "ollama":
                    raise ProviderRegistryError(
                        "provider_credential_not_required",
                        "内置 Provider 无需凭据",
                    )
                removed = self.secret_store.delete(config.provider_id)
                close_slots.extend(self._retire_instance_locked(config.provider_id))
                return removed

        return await self._run_sync_update(delete_sync, close_slots)

    def resolve(self, provider_id: str = "ollama") -> ModelProvider:
        """返回当前实例但不延长生命周期；长任务应使用 ``lease``。"""
        with self._lock:
            config = self.get_config(provider_id)
            return self._get_or_create_slot_locked(config).provider

    def lease(self, provider_id: str = "ollama") -> ProviderLease:
        """获取绑定当前 Provider 版本的租约。调用方必须释放或使用 async with。"""
        with self._lock:
            config = self.get_config(provider_id)
            slot = self._get_or_create_slot_locked(config)
            slot.leases += 1
        return ProviderLease(self, slot)

    async def acquire_lease(self, provider_id: str = "ollama") -> ProviderLease:
        """在线程中完成首次实例构建与密钥解密，避免阻塞事件循环。"""

        return await acquire_critical(
            asyncio.to_thread(self.lease, provider_id),
            lambda lease: lease.release(),
        )

    async def list_models(
        self,
        provider_id: str = "ollama",
        *,
        refresh: bool = False,
    ) -> list[str]:
        with self._lock:
            config = self.get_config(provider_id)
            if config.models and not refresh:
                return sorted(set(config.models))
        lease = await self.acquire_lease(config.provider_id)
        async with lease as provider:
            return await provider.list_models()

    async def test(self, provider_id: str) -> dict[str, object]:
        config = self.get_config(provider_id)
        lease = await self.acquire_lease(config.provider_id)
        async with lease as provider:
            models = await provider.list_models()
            return {
                "ok": True,
                "provider_id": config.provider_id,
                "models": models,
                "model_count": len(models),
                "capabilities": provider.capabilities.as_public(),
            }

    async def close(self) -> None:
        with self._lock:
            self._closed = True
            instances = [*self._instances.values(), *self._retired_instances]
            self._instances.clear()
            self._retired_instances.clear()
            close_slots = []
            for slot in instances:
                slot.retired = True
                if not slot.close_started:
                    slot.close_started = True
                    close_slots.append(slot)
        await await_critical(self._close_slots(close_slots))


_registry: ProviderRegistry | None = None


def get_provider_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry


async def close_provider_registry() -> None:
    """关闭已创建的 Provider 实例；未使用过时不为关闭动作创建注册表。"""
    global _registry
    previous = _registry
    _registry = None
    if previous is not None:
        await previous.close()


async def reset_provider_registry_for_testing(
    registry: ProviderRegistry | None = None,
) -> None:
    global _registry
    previous = _registry
    _registry = registry
    if previous is not None and previous is not registry:
        await previous.close()
