"""受隔离环境保护的桌面端到端旅程测试支持。

此模块不会被普通桌面启动路径激活。调用方必须同时提供专用 CLI 参数、
``TAVERN_DESKTOP_JOURNEY_TEST=1``，并把全部可写路径放在系统临时目录内。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


JOURNEY_SCHEMA_VERSION = 1
JOURNEY_TIMEOUT_MS = 90_000
JOURNEY_TITLE_PREFIX = "__LOCAL_TAVERN_JOURNEY_V1__"
JOURNEY_ENVIRONMENT_FLAG = "TAVERN_DESKTOP_JOURNEY_TEST"
JOURNEY_ROOT_PREFIX = "local-tavern-desktop-journey-"
JOURNEY_PROVIDER_ID = "desktop-journey-fake"
JOURNEY_MODEL = "journey-model"
JOURNEY_PROJECT = "desktop-journey"
JOURNEY_PRIMARY_SAVE = "journey-primary"
JOURNEY_SECONDARY_SAVE = "journey-secondary"
JOURNEY_USER_MARKER = "desktop journey user message"
JOURNEY_ASSISTANT_MARKER = "desktop journey assistant response"
_JOURNEY_WRITABLE_ENVIRONMENTS = (
    "TAVERN_DESKTOP_USER_ROOT",
    "TAVERN_DATA_DIR",
    "TAVERN_SETTINGS_PATH",
    "TAVERN_PROJECTS_DIR",
    "TAVERN_PROMPTS_DIR",
    "TAVERN_RECOVERY_DIR",
    "TAVERN_MIGRATIONS_DIR",
    "TAVERN_BACKUP_DIR",
    "TAVERN_LOG_DIR",
    "TAVERN_LOG_FILE",
    "TAVERN_PID_PATH",
    "TAVERN_STOP_REQUEST_PATH",
    "TAVERN_PROVIDER_DATA_DIR",
    "TAVERN_PROVIDER_CONFIG_PATH",
    "TAVERN_PROVIDER_SECRETS_PATH",
)


class DesktopJourneyError(RuntimeError):
    """旅程测试契约或运行状态不符合预期。"""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def validate_journey_paths(result_path: Path, screenshot_path: Path) -> Path:
    """验证测试钩子只会读写系统临时目录中的专用根目录。"""
    if os.environ.get(JOURNEY_ENVIRONMENT_FLAG) != "1":
        raise DesktopJourneyError("桌面旅程测试环境开关未启用")
    configured_root = os.environ.get("TAVERN_BASE_DIR", "").strip()
    if not configured_root:
        raise DesktopJourneyError("桌面旅程测试缺少隔离根目录")
    root = Path(configured_root).expanduser().resolve(strict=False)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    if root.name.startswith(JOURNEY_ROOT_PREFIX) is False or not _is_within(root, temp_root):
        raise DesktopJourneyError("桌面旅程测试根目录不符合隔离契约")
    for name in _JOURNEY_WRITABLE_ENVIRONMENTS:
        configured = os.environ.get(name, "").strip()
        if configured and not _is_within(Path(configured).expanduser(), root):
            raise DesktopJourneyError(f"桌面旅程测试可写路径越界：{name}")
    for label, raw_path in (
        ("结果", result_path),
        ("截图", screenshot_path),
    ):
        target = Path(raw_path).expanduser()
        if not target.is_absolute() or not _is_within(target, root):
            raise DesktopJourneyError(f"桌面旅程测试{label}路径必须位于隔离根目录")
    return root


@dataclass(frozen=True, slots=True)
class _JourneyProviderConfig:
    provider_id: str
    kind: str
    name: str
    base_url: str
    context_limit: int
    models: tuple[str, ...]
    preset: str = "custom"

    def as_public(self) -> dict[str, object]:
        from core.model_provider import ProviderCapabilities

        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "name": self.name,
            "base_url": self.base_url,
            "context_limit": self.context_limit,
            "models": list(self.models),
            "preset": self.preset,
            "credential_required": False,
            "has_credential": True,
            "capabilities": ProviderCapabilities().as_public(),
        }


class _JourneyProvider:
    def __init__(self) -> None:
        from core.model_provider import ProviderCapabilities

        self.capabilities = ProviderCapabilities()

    async def list_models(self) -> list[str]:
        return [JOURNEY_MODEL]

    async def get_context_limit(self, model: str) -> dict[str, object]:
        if model != JOURNEY_MODEL:
            raise DesktopJourneyError("旅程测试请求了未知模型")
        return {"context_limit": 16_384, "source": "desktop_journey_fake"}

    async def chat_stream(self, model: str, messages: list[dict], **_kwargs):
        if model != JOURNEY_MODEL or not isinstance(messages, list):
            raise DesktopJourneyError("旅程测试聊天请求无效")
        await asyncio.sleep(0)
        yield {"type": "content", "content": JOURNEY_ASSISTANT_MARKER}
        yield {"type": "done", "content": ""}

    async def summarize_once(
        self,
        model: str,
        dropped_messages: list[dict],
        **_kwargs,
    ) -> str:
        del dropped_messages
        if model != JOURNEY_MODEL:
            raise DesktopJourneyError("旅程测试摘要请求了未知模型")
        return "前情提要(必填,1-3句纯文本): desktop journey summary"

    async def close(self) -> None:
        return None


class _JourneyProviderLease:
    def __init__(self, config: _JourneyProviderConfig, provider: _JourneyProvider) -> None:
        self.config = config
        self.provider = provider
        self.provider_id = config.provider_id
        self._released = False

    async def release(self) -> None:
        self._released = True

    async def __aenter__(self):
        return self.provider

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self.release()


class JourneyProviderRegistry:
    """完全内存、零网络的测试 Provider 注册表。"""

    def __init__(self) -> None:
        self._provider = _JourneyProvider()
        self._configs = {
            JOURNEY_PROVIDER_ID: _JourneyProviderConfig(
                provider_id=JOURNEY_PROVIDER_ID,
                kind="openai_compatible",
                name="Desktop Journey Fake",
                base_url="https://journey.invalid/v1",
                context_limit=16_384,
                models=(JOURNEY_MODEL,),
            ),
            "ollama": _JourneyProviderConfig(
                provider_id="ollama",
                kind="ollama",
                name="Ollama (journey fake)",
                base_url="http://127.0.0.1:1",
                context_limit=16_384,
                models=(JOURNEY_MODEL,),
            ),
        }

    @staticmethod
    def presets() -> list[dict[str, object]]:
        from core.provider_registry import ProviderRegistry

        return ProviderRegistry.presets()

    def list_configs(self) -> list[dict[str, object]]:
        return [self._configs[key].as_public() for key in sorted(self._configs)]

    def get_config(self, provider_id: str) -> _JourneyProviderConfig:
        try:
            return self._configs[provider_id]
        except KeyError as exc:
            from core.provider_registry import ProviderRegistryError

            raise ProviderRegistryError("provider_not_found", "Provider 不存在") from exc

    def public_config(self, provider_id: str) -> dict[str, object]:
        return self.get_config(provider_id).as_public()

    def resolve(self, provider_id: str = "ollama") -> _JourneyProvider:
        self.get_config(provider_id)
        return self._provider

    def lease(self, provider_id: str = "ollama") -> _JourneyProviderLease:
        return _JourneyProviderLease(self.get_config(provider_id), self._provider)

    async def acquire_lease(
        self,
        provider_id: str = "ollama",
    ) -> _JourneyProviderLease:
        """匹配正式注册表的异步租约接口；内存替身无需线程切换。"""

        return self.lease(provider_id)

    async def list_models(self, provider_id: str) -> list[str]:
        self.get_config(provider_id)
        return await self._provider.list_models()

    async def test(self, provider_id: str) -> dict[str, object]:
        config = self.get_config(provider_id)
        return {
            "ok": True,
            "provider_id": config.provider_id,
            "models": list(config.models),
            "model_count": len(config.models),
            "capabilities": self._provider.capabilities.as_public(),
        }

    async def close(self) -> None:
        await self._provider.close()


def install_journey_provider_registry() -> JourneyProviderRegistry:
    """在 server 导入后、ASGI lifespan 启动前安装测试注册表。"""
    if os.environ.get(JOURNEY_ENVIRONMENT_FLAG) != "1":
        raise DesktopJourneyError("拒绝在普通桌面启动中安装旅程测试 Provider")
    from core import provider_registry

    if provider_registry._registry is not None:  # noqa: SLF001 - 专用测试注入点
        raise DesktopJourneyError("Provider 注册表已初始化，无法安全安装测试替身")
    registry = JourneyProviderRegistry()
    provider_registry._registry = registry  # type: ignore[assignment]  # noqa: SLF001
    return registry


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)


def base_journey_result(stage: str) -> dict[str, Any]:
    return {
        "schema_version": JOURNEY_SCHEMA_VERSION,
        "pid": os.getpid(),
        "stage": stage,
        "ready": False,
        "page_loaded": False,
        "api_live": False,
        "project_created": False,
        "saves_created": False,
        "chat_completed": False,
        "refresh_verified": False,
        "save_switch_verified": False,
        "persistence_verified": False,
        "message_count": 0,
        "screenshot_saved": False,
        "runtime": "in_process_asgi",
        "transport": "qwebchannel",
        "scheme": "tavern://app",
        "tcp_listener_started": False,
        "off_the_record": False,
        "error": "",
    }


def write_journey_failure(path: Path, stage: str, error: str) -> None:
    result = base_journey_result(stage)
    result["error"] = str(error)[:120]
    _atomic_write_json(path, result)


def journey_probe_script(stage: str) -> str:
    """生成只使用正式 QWebChannel transport 与正式 API 的旅程驱动脚本。"""
    constants = {
        "prefix": JOURNEY_TITLE_PREFIX,
        "stage": stage,
        "provider": JOURNEY_PROVIDER_ID,
        "model": JOURNEY_MODEL,
        "project": JOURNEY_PROJECT,
        "primary": JOURNEY_PRIMARY_SAVE,
        "secondary": JOURNEY_SECONDARY_SAVE,
        "userMarker": JOURNEY_USER_MARKER,
        "assistantMarker": JOURNEY_ASSISTANT_MARKER,
    }
    encoded_constants = json.dumps(constants, ensure_ascii=False)
    return f"""
        (() => {{
            if (globalThis.__localTavernJourneyInstalled) return;
            globalThis.__localTavernJourneyInstalled = true;
            const cfg = {encoded_constants};
            const stateKey = `local-tavern-journey-${{cfg.stage}}`;
            const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
            const waitUntil = async (probe, code, attempts = 300) => {{
                for (let index = 0; index < attempts; index += 1) {{
                    const value = await probe();
                    if (value) return value;
                    await sleep(50);
                }}
                throw new Error(code);
            }};
            const report = payload => {{
                const bytes = unescape(encodeURIComponent(JSON.stringify(payload)));
                console.info(cfg.prefix + btoa(bytes));
            }};
            const parseResponse = async response => {{
                const text = await response.text();
                let body = null;
                try {{ body = text ? JSON.parse(text) : null; }} catch (_error) {{}}
                if (!response.ok) {{
                    const detail = body && body.detail;
                    const code = detail && typeof detail === 'object' ? detail.code : '';
                    throw new Error(code || `http_${{response.status}}`);
                }}
                return body;
            }};
            const api = async (path, options = {{}}) => {{
                const headers = new Headers(options.headers || {{}});
                let body = options.body;
                if (body && typeof body !== 'string') {{
                    headers.set('content-type', 'application/json');
                    body = JSON.stringify(body);
                }}
                return parseResponse(await globalThis.TavernDesktopTransport.fetch(path, {{
                    ...options, headers, body, cache: 'no-store',
                }}));
            }};
            const getSession = save => api(
                `/api/session?project=${{encodeURIComponent(cfg.project)}}&save=${{encodeURIComponent(save)}}`,
            );
            const ensureProjectAndSaves = async () => {{
                const projects = await api('/api/projects');
                if (!projects.projects.includes(cfg.project)) {{
                    await api('/api/projects', {{ method: 'POST', body: {{ name: cfg.project }} }});
                }}
                const sessions = await api(`/api/sessions?project=${{encodeURIComponent(cfg.project)}}`);
                const ids = new Set(sessions.sessions.map(item => item.session_id));
                for (const save of [cfg.primary, cfg.secondary]) {{
                    if (!ids.has(save)) {{
                        await api('/api/sessions', {{
                            method: 'POST', body: {{ project: cfg.project, name: save }},
                        }});
                    }}
                }}
            }};
            const ensureChat = async () => {{
                let session = await getSession(cfg.primary);
                const history = Array.isArray(session.message_history) ? session.message_history : [];
                if (!history.some(item => item.content === cfg.assistantMarker)) {{
                    const turn = await api('/api/chat/turns', {{
                        method: 'POST',
                        body: {{
                            project: cfg.project,
                            save: cfg.primary,
                            user_input: cfg.userMarker,
                            provider: cfg.provider,
                            model: cfg.model,
                            expected_revision: session.revision,
                            think: false,
                            num_predict: 128,
                        }},
                    }});
                    const terminal = await waitUntil(async () => {{
                        const value = await api(`/api/chat/turns/${{turn.turn_id}}`);
                        return ['completed', 'failed', 'cancelled'].includes(value.status) ? value : null;
                    }}, 'turn_timeout');
                    if (terminal.status !== 'completed') throw new Error(`turn_${{terminal.status}}`);
                    session = await getSession(cfg.primary);
                }}
                return session;
            }};
            const verifyBackend = async () => {{
                const projects = await api('/api/projects');
                if (!projects.projects.includes(cfg.project)) throw new Error('project_missing');
                const saves = await api(`/api/sessions?project=${{encodeURIComponent(cfg.project)}}`);
                const ids = new Set(saves.sessions.map(item => item.session_id));
                if (!ids.has(cfg.primary) || !ids.has(cfg.secondary)) throw new Error('save_missing');
                const primary = await getSession(cfg.primary);
                const secondary = await getSession(cfg.secondary);
                const history = Array.isArray(primary.message_history) ? primary.message_history : [];
                if (!history.some(item => item.content === cfg.userMarker)) throw new Error('user_message_missing');
                if (!history.some(item => item.content === cfg.assistantMarker)) throw new Error('assistant_message_missing');
                if ((secondary.message_history || []).length !== 0) throw new Error('save_isolation_failed');
                return history.length;
            }};
            const selectSaveInUi = async save => {{
                const tab = await waitUntil(() => document.getElementById('tab-saves'), 'save_tab_missing');
                tab.click();
                const option = await waitUntil(
                    () => document.querySelector(`#save-list [data-save="${{CSS.escape(save)}}"]`),
                    `save_option_missing_${{save}}`,
                );
                option.click();
                await waitUntil(() => {{
                    const label = document.getElementById('current-session-label');
                    return label && label.textContent.includes(save);
                }}, `save_switch_failed_${{save}}`);
            }};
            const completeOnboarding = async () => {{
                const backdrop = document.getElementById('modal-backdrop');
                if (!backdrop || backdrop.classList.contains('hidden')) return;
                const title = document.getElementById('modal-title');
                if (!title || title.textContent.trim() !== '欢迎使用本地酒馆') {{
                    throw new Error('unexpected_blocking_modal');
                }}
                const chooseLocal = document.querySelector(
                    '.onboarding-card.recommended .primary-btn',
                );
                if (!chooseLocal) throw new Error('onboarding_local_action_missing');
                chooseLocal.click();
                await waitUntil(
                    () => backdrop.classList.contains('hidden'),
                    'onboarding_close_failed',
                    100,
                );
                if (localStorage.getItem('local-tavern.onboarding.v1') !== 'complete') {{
                    throw new Error('onboarding_completion_missing');
                }}
            }};
            const verifyUi = async () => {{
                await waitUntil(() => {{
                    const label = document.getElementById('current-session-label');
                    return label && label.textContent.includes(cfg.project);
                }}, 'workspace_not_ready');
                await selectSaveInUi(cfg.secondary);
                await selectSaveInUi(cfg.primary);
                await waitUntil(() => {{
                    const stream = document.getElementById('chat-stream');
                    return stream && stream.textContent.includes(cfg.assistantMarker);
                }}, 'chat_not_rendered');
            }};
            const run = async () => {{
                await waitUntil(
                    () => globalThis.TavernDesktopTransport
                        && typeof globalThis.TavernDesktopTransport.ready === 'function'
                        && typeof globalThis.TavernDesktopTransport.fetch === 'function',
                    'desktop_transport_missing',
                    1000,
                );
                await globalThis.TavernDesktopTransport.ready();
                const live = await api('/health/live');
                if (!live || live.status !== 'alive') throw new Error('api_live_failed');

                const marker = sessionStorage.getItem(stateKey);
                if (cfg.stage === 'seed' && marker !== 'seeded') {{
                    await ensureProjectAndSaves();
                    await ensureChat();
                    sessionStorage.setItem(stateKey, 'seeded');
                    location.hash = cfg.project;
                    location.reload();
                    return;
                }}
                if (location.hash.slice(1) !== cfg.project) {{
                    sessionStorage.setItem(stateKey, marker || 'reloaded');
                    location.hash = cfg.project;
                    location.reload();
                    return;
                }}
                const messageCount = await verifyBackend();
                await completeOnboarding();
                await verifyUi();
                await new Promise(resolve => requestAnimationFrame(
                    () => requestAnimationFrame(resolve),
                ));
                await sleep(100);
                report({{
                    ok: true,
                    api_live: true,
                    project_created: true,
                    saves_created: true,
                    chat_completed: true,
                    refresh_verified: true,
                    save_switch_verified: true,
                    persistence_verified: cfg.stage === 'verify',
                    message_count: messageCount,
                    error: '',
                }});
            }};
            void run().catch(error => report({{
                ok: false,
                api_live: false,
                error: error && error.message ? String(error.message).slice(0, 100) : 'journey_failed',
            }}));
        }})();
    """


class JourneyController:
    """监听页面报告、保存真实窗口截图并结束当前测试阶段。"""

    def __init__(
        self,
        application,
        window,
        result_path: Path,
        screenshot_path: Path,
        stage: str,
        hold_ms: int,
    ) -> None:
        from PySide6.QtCore import QObject

        self._owner = QObject(window)
        self._application = application
        self._window = window
        self._result_path = Path(result_path)
        self._screenshot_path = Path(screenshot_path)
        self._stage = stage
        self._hold_ms = hold_ms
        self._finished = False
        self.success = False

    def start(self) -> None:
        from PySide6.QtCore import QTimer

        self._window.page.consoleMessage.connect(self._console_message)
        self._window.install_document_script(
            f"local-tavern-journey-{self._stage}-v1",
            journey_probe_script(self._stage),
        )
        QTimer.singleShot(JOURNEY_TIMEOUT_MS, self._timeout)

    def _console_message(self, message: str) -> None:
        if self._finished or not message.startswith(JOURNEY_TITLE_PREFIX):
            return
        try:
            encoded = message[len(JOURNEY_TITLE_PREFIX) :]
            parsed = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            parsed = {"ok": False, "error": "invalid_journey_result"}
        self._finish(parsed if isinstance(parsed, dict) else {})

    def _timeout(self) -> None:
        if self._finished:
            return
        diagnostic = self._window.last_diagnostic
        error = "journey_timeout" if not diagnostic else f"journey_timeout:{diagnostic}"
        self._finish({"ok": False, "error": error})

    def _finish(self, page_result: dict[str, Any]) -> None:
        from PySide6.QtCore import QTimer

        if self._finished:
            return
        self._finished = True
        result = base_journey_result(self._stage)
        error = str(page_result.get("error") or "")[:120]
        if not page_result.get("ok") and not error:
            error = "journey_page_failed"
        screenshot_saved = False
        if not error:
            self._screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            pixmap = self._window.grab()
            screenshot_saved = bool(
                not pixmap.isNull()
                and pixmap.save(str(self._screenshot_path), "PNG")
                and self._screenshot_path.is_file()
                and self._screenshot_path.stat().st_size > 0
            )
            if not screenshot_saved:
                error = "screenshot_failed"
        self.success = not error
        result.update({
            "ready": self.success,
            "page_loaded": bool(page_result.get("ok")),
            "api_live": bool(page_result.get("api_live")) and self.success,
            "project_created": bool(page_result.get("project_created")),
            "saves_created": bool(page_result.get("saves_created")),
            "chat_completed": bool(page_result.get("chat_completed")),
            "refresh_verified": bool(page_result.get("refresh_verified")),
            "save_switch_verified": bool(page_result.get("save_switch_verified")),
            "persistence_verified": bool(page_result.get("persistence_verified")),
            "message_count": int(page_result.get("message_count", 0) or 0),
            "screenshot_saved": screenshot_saved,
            "off_the_record": bool(self._window.is_off_the_record),
            "error": error,
        })
        _atomic_write_json(self._result_path, result)
        QTimer.singleShot(self._hold_ms, self._application.quit)
