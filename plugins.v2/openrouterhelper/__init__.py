"""
MoviePilot 插件：OpenRouter 免费模型助手 (OpenRouterHelper)

功能：
    1. 自动从 OpenRouter 拉取模型目录，识别其中的免费模型
       （pricing.prompt == 0 且 pricing.completion == 0，排除音频生成等非对话模型）。
    2. 将选定的免费模型一键填充到 MoviePilot「智能助手 / LLM」配置：
           LLM_PROVIDER / LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / LLM_API_PROTOCOL
       可选同时打开 AI_AGENT_ENABLE。
    3. 标注每个免费模型是否支持 tool calling（智能助手靠它调工具，强烈建议只选支持的）。
    4. 支持写入 app.env 持久化（重启后仍生效），支持定时自动刷新模型目录。

实测边界（2026-10）：
    - GET https://openrouter.ai/api/v1/models 无需鉴权即可拉取（458 个模型，其中 19 个零价、16 个支持 tools）。
    - 调用 :free 模型必须携带 OpenRouter API Key（https://openrouter.ai/keys），否则返回 401。
      OpenRouter 免费额度随账号余额变化：未充值约 50 次/天，累计充值 ≥10 美元后约 1000 次/天。
    - 免费模型 id 形如 `vendor/model:free`，但 `openrouter/free`、`inclusionai/ling-3.1-flash`
      这类零价模型不带 :free 后缀，所以本插件按「零价」判定而不是按后缀。

依赖：requests（MoviePilot 自带）
"""

import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.log import logger

# --- 宿主基类：兼容 v2 与 ADR-0008 归位后的 SDK 路径 ---
try:
    from app.plugins import _PluginBase
except ImportError:  # pragma: no cover - 新版本归位到 SDK
    from app.sdk.plugin.base import _PluginBase

# --- 运行配置对象：兼容 v2(app.core.config) 与 v3(app.runtime.config) ---
try:
    from app.core.config import settings
except ImportError:  # pragma: no cover
    from app.runtime.config import settings

# --- 模块管理器：用于改配置后热重载 ---
try:
    from app.core.module import ModuleManager
except ImportError:  # pragma: no cover
    ModuleManager = None

# --- 定时任务触发器 ---
try:
    from apscheduler.triggers.cron import CronTrigger
except ImportError:  # pragma: no cover
    CronTrigger = None

# --- 写入 app.env ---
try:
    from dotenv import set_key
except ImportError:  # pragma: no cover
    set_key = None


# ============================ 常量 ============================

# 模型目录接口（OpenRouter，无需鉴权）
DEFAULT_LIST_URL = "https://openrouter.ai/api/v1/models"
# OpenAI 兼容对话接口前缀
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
# MoviePilot 智能助手的提供商标识
DEFAULT_PROVIDER = "openrouter"
# 需要写回智能助手的配置键
LLM_KEYS = (
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_API_PROTOCOL",
)


class OpenRouterHelper(_PluginBase):
    # ---------- 插件元信息 ----------
    plugin_name = "OpenRouter 免费模型助手"
    plugin_desc = "自动获取 OpenRouter 免费模型，并一键填充到智能助手(LLM)配置。"
    plugin_icon = "openrouterhelper.png"
    plugin_version = "1.0.0"
    plugin_author = "wangh"
    author_url = "https://github.com/wj-wyt"
    plugin_config_prefix = "openrouterhelper_"
    plugin_order = 20
    auth_level = 1

    # ---------- 运行态 ----------
    _enabled: bool = False
    _auto_apply: bool = False
    _persist_env: bool = False
    _enable_agent: bool = False
    _use_proxy: bool = False
    _only_tool_models: bool = True

    _api_key: str = ""
    _base_url: str = DEFAULT_BASE_URL
    _list_url: str = DEFAULT_LIST_URL
    _provider: str = DEFAULT_PROVIDER
    _protocol: str = "chat_completions"
    _model: str = ""
    _refresh_hours: int = 12
    _site_url: str = ""
    _site_name: str = "MoviePilot"

    _models_cache: List[dict] = []
    _last_refresh: Optional[float] = None

    # ======================= 生命周期 =======================

    def init_plugin(self, config: dict = None):
        config = config or {}

        self._enabled = bool(config.get("enabled"))
        self._auto_apply = bool(config.get("auto_apply"))
        self._persist_env = bool(config.get("persist_env"))
        self._enable_agent = bool(config.get("enable_agent"))
        self._use_proxy = bool(config.get("use_proxy"))
        self._only_tool_models = bool(config.get("only_tool_models", True))

        self._api_key = str(config.get("api_key") or "").strip()
        self._base_url = str(config.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
        self._list_url = str(config.get("list_url") or DEFAULT_LIST_URL).strip()
        self._provider = str(config.get("provider") or DEFAULT_PROVIDER).strip()
        self._protocol = str(config.get("protocol") or "chat_completions").strip()
        self._model = str(config.get("model") or "").strip()
        self._site_url = str(config.get("site_url") or "").strip()
        self._site_name = str(config.get("site_name") or "MoviePilot").strip()
        try:
            self._refresh_hours = int(config.get("refresh_hours") or 12)
        except (TypeError, ValueError):
            self._refresh_hours = 12

        # 载入本地缓存（避免启动即发网络请求）
        cache = self.get_data("models_cache") or {}
        self._models_cache = cache.get("models") or []
        self._last_refresh = cache.get("ts")

        if not self._enabled:
            return

        # 保存时若勾选自动填充 → 幂等地写回智能助手
        if self._auto_apply and self._model:
            try:
                self.apply_to_agent()
            except Exception as err:  # noqa: BLE001
                logger.error(f"OpenRouterHelper 自动填充智能助手失败：{err}")

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        """无常驻线程，无需额外清理。"""
        pass

    # ======================= 网络与模型 =======================

    def _proxies(self) -> Optional[dict]:
        if not self._use_proxy:
            return None
        proxy = getattr(settings, "PROXY_HOST", None)
        if not proxy:
            return None
        return {"http": proxy, "https": proxy}

    def _headers(self, json_body: bool = False) -> dict:
        headers = {
            "Accept": "application/json",
            "User-Agent": "MoviePilot-OpenRouterHelper/1.0 (+https://movie-pilot.org)",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # OpenRouter 官方建议带的归属头（用于应用排行，可选）
        if self._site_url:
            headers["HTTP-Referer"] = self._site_url
        if self._site_name:
            headers["X-Title"] = self._site_name
        return headers

    # ---------- 免费模型判定 ----------

    @staticmethod
    def _is_zero(value: Any) -> bool:
        try:
            return float(value or 0) == 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _outputs_text_only(architecture: dict) -> bool:
        """是否「纯文本」输出：用于排除 lyria 等音频/图像生成模型。

        实测样本中 lyria 的 output_modalities 为 ["text", "audio"]，
        它虽然也能吐文本，但本质是音乐生成模型，不适合塞给智能助手。
        """
        arch = architecture or {}
        outs = arch.get("output_modalities")
        if isinstance(outs, list) and outs:
            return "text" in outs and not ({"audio", "image"} & set(outs))
        modality = str(arch.get("modality") or "")
        if "->" in modality:
            out = modality.split("->", 1)[1]
            return "text" in out and "audio" not in out and "image" not in out
        return True  # 信息缺失时不排除

    @classmethod
    def _is_free(cls, item: dict) -> bool:
        """零价 + 纯文本输出 = 免费可用的对话模型。"""
        pricing = item.get("pricing") or {}
        if not (cls._is_zero(pricing.get("prompt")) and cls._is_zero(pricing.get("completion"))):
            return False
        return cls._outputs_text_only(item.get("architecture") or {})

    @staticmethod
    def _supports_tools(item: dict) -> bool:
        params = item.get("supported_parameters") or []
        return "tools" in params

    @staticmethod
    def _pretty_name(model_id: str, raw_name: str = "") -> str:
        if raw_name:
            return raw_name
        base = model_id[: -len(":free")] if model_id.endswith(":free") else model_id
        vendor, _, short = base.partition("/")
        return f"{vendor} {short}" if short else base

    def _parse_models(self, payload: dict) -> List[dict]:
        raw_items = payload.get("data") or payload.get("models") or []
        models: List[dict] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            mid = item.get("id")
            if not mid or not self._is_free(item):
                continue
            has_tools = self._supports_tools(item)
            if self._only_tool_models and not has_tools:
                continue
            models.append(
                {
                    "id": mid,
                    "name": self._pretty_name(mid, item.get("name") or ""),
                    "context": item.get("context_length"),
                    "tools": has_tools,
                }
            )
        # 支持工具调用的排前面，再按上下文长度降序
        models.sort(key=lambda m: (not m["tools"], -(m["context"] or 0), m["id"]))
        return models

    def _fetch_models(self) -> List[dict]:
        """请求上游模型目录，返回免费模型列表。"""
        resp = requests.get(
            self._list_url,
            headers=self._headers(),
            proxies=self._proxies(),
            timeout=30,
        )
        resp.raise_for_status()
        return self._parse_models(resp.json())

    def refresh_models(self, force: bool = False) -> List[dict]:
        """刷新免费模型列表，带 TTL 缓存。"""
        now = time.time()
        ttl = max(self._refresh_hours, 0) * 3600
        if (
            not force
            and self._models_cache
            and self._last_refresh
            and (now - self._last_refresh) < ttl
        ):
            return self._models_cache

        try:
            data = self._fetch_models()
        except Exception as err:  # noqa: BLE001
            logger.error(f"OpenRouterHelper 拉取模型目录失败：{err}")
            return self._models_cache

        if data:
            self._models_cache = data
            self._last_refresh = now
            self.save_data("models_cache", {"models": data, "ts": now})
            logger.info(f"OpenRouterHelper 已更新免费模型列表，共 {len(data)} 个")

        # 自动兜底：当前选中的模型已不在免费列表里（下架/转付费）
        if data and self._model and self._model not in {m["id"] for m in data}:
            logger.warn(
                f"OpenRouterHelper 当前选中的模型 {self._model} 已不在免费列表中，请重新选择"
            )
        return self._models_cache

    # ======================= 写回智能助手 =======================

    def _target_values(self) -> Dict[str, Any]:
        values: Dict[str, Any] = {
            "LLM_PROVIDER": self._provider,
            "LLM_BASE_URL": self._base_url,
            "LLM_API_KEY": self._api_key,
            "LLM_MODEL": self._model,
            "LLM_API_PROTOCOL": self._protocol,
        }
        if self._enable_agent:
            values["AI_AGENT_ENABLE"] = True
        return values

    def _write_env(self, values: Dict[str, Any]):
        """把配置持久化到 app.env（重启后仍生效）。"""
        if not set_key:
            logger.warn("OpenRouterHelper 未安装 python-dotenv，跳过 app.env 持久化")
            return
        try:
            env_path = settings.CONFIG_PATH / "app.env"
            env_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as err:  # noqa: BLE001
            logger.error(f"OpenRouterHelper 准备 app.env 路径失败：{err}")
            return
        for key, value in values.items():
            set_key(str(env_path), key, "" if value is None else str(value))
        logger.info(f"OpenRouterHelper 已写入 app.env：{list(values.keys())}")

    def _reload_modules(self):
        if not ModuleManager:
            return
        try:
            ModuleManager().stop()
            ModuleManager().load_modules()
        except Exception as err:  # noqa: BLE001
            logger.error(f"OpenRouterHelper 模块重载失败：{err}")

    def apply_to_agent(self) -> Dict[str, Any]:
        """把当前选择填充到智能助手配置（幂等：无变化则不重载模块）。"""
        if not self._model:
            raise ValueError("请先选择要使用的免费模型")

        values = self._target_values()
        changed = {
            key: value
            for key, value in values.items()
            if str(getattr(settings, key, None)) != str(value)
        }

        if not changed:
            logger.info("OpenRouterHelper 智能助手配置已是目标值，无需改写")
            return values

        # 1) 运行时生效
        for key, value in changed.items():
            try:
                setattr(settings, key, value)
            except Exception as err:  # noqa: BLE001
                logger.warn(f"OpenRouterHelper 设置 {key} 失败：{err}")

        # 2) 持久化
        if self._persist_env:
            self._write_env(changed)

        # 3) 热重载
        self._reload_modules()

        logger.info(
            f"OpenRouterHelper 已填充智能助手：provider={self._provider} "
            f"model={self._model} base={self._base_url}（变更 {list(changed.keys())}）"
        )
        return values

    # ======================= 插件 API =======================

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/models",
                "endpoint": self.api_models,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取免费模型列表",
                "description": "返回 OpenRouter 当前可用的免费模型",
            },
            {
                "path": "/refresh",
                "endpoint": self.api_refresh,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "刷新免费模型列表",
                "description": "强制重新从 OpenRouter 拉取模型目录",
            },
            {
                "path": "/apply",
                "endpoint": self.api_apply,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "填充到智能助手",
                "description": "把当前选择的免费模型写入 LLM 配置",
            },
            {
                "path": "/test",
                "endpoint": self.api_test,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "连通性测试",
                "description": "向所选模型发送一条测试消息，验证 Key 与模型是否可用",
            },
        ]

    def api_models(self) -> Dict[str, Any]:
        return {
            "success": True,
            "message": "ok",
            "data": {
                "models": self._models_cache,
                "last_refresh": self._last_refresh,
                "count": len(self._models_cache),
            },
        }

    def api_refresh(self) -> Dict[str, Any]:
        models = self.refresh_models(force=True)
        return {
            "success": bool(models),
            "message": f"已获取 {len(models)} 个免费模型" if models else "拉取失败，请检查网络或接口地址",
            "data": models,
        }

    def api_apply(self) -> Dict[str, Any]:
        try:
            values = self.apply_to_agent()
            return {"success": True, "message": "已填充到智能助手", "data": values}
        except Exception as err:  # noqa: BLE001
            return {"success": False, "message": str(err), "data": None}

    def api_test(self) -> Dict[str, Any]:
        model = self._model
        if not model:
            return {"success": False, "message": "请先选择模型", "data": None}
        try:
            resp = requests.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(json_body=True),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "reply with: ok"}],
                    "max_tokens": 16,
                },
                proxies=self._proxies(),
                timeout=60,
            )
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                return {
                    "success": False,
                    "message": f"HTTP {resp.status_code}：{resp.text[:200]}",
                    "data": None,
                }

            if resp.status_code == 200 and payload.get("choices"):
                content = (
                    (payload["choices"][0].get("message") or {}).get("content") or ""
                ).strip()
                return {
                    "success": True,
                    "message": f"模型 {model} 调用成功：{content}",
                    "data": payload,
                }

            error = payload.get("error") or {}
            tip = error.get("message") or str(payload)[:300]
            if resp.status_code == 401:
                tip += "（请在 https://openrouter.ai/keys 创建并填写 API Key）"
            elif resp.status_code == 429:
                tip += "（免费额度已用尽，可稍后重试或为 OpenRouter 账号充值以提高配额）"
            return {"success": False, "message": f"调用失败：{tip}", "data": payload}
        except Exception as err:  # noqa: BLE001
            return {"success": False, "message": f"请求异常：{err}", "data": None}

    # ======================= 定时服务 =======================

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or self._refresh_hours <= 0 or not CronTrigger:
            return []
        if self._refresh_hours >= 24:
            crontab = "0 3 * * *"
        else:
            crontab = f"0 */{max(1, self._refresh_hours)} * * *"
        return [
            {
                "id": "OpenRouterHelper.Refresh",
                "name": "OpenRouter 免费模型自动刷新",
                "trigger": CronTrigger.from_crontab(crontab),
                "func": self.service_refresh,
                "kwargs": {},
            }
        ]

    def service_refresh(self):
        models = self.refresh_models(force=True)
        if models and self._auto_apply and self._model:
            try:
                self.apply_to_agent()
            except Exception as err:  # noqa: BLE001
                logger.error(f"OpenRouterHelper 定时填充失败：{err}")

    # ======================= 配置页 / 详情页 =======================

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 首次打开配置页且无缓存时，尽力拉一次（用户主动操作，允许短暂等待）
        cache = getattr(self, "_models_cache", None) or []
        if not cache:
            try:
                cache = self.refresh_models(force=True)
            except Exception as err:  # noqa: BLE001
                logger.warn(f"OpenRouterHelper 配置页加载模型失败：{err}")
                cache = []

        model_items = []
        for m in cache:
            ctx = m.get("context") or 0
            ctx_text = f"{round(ctx / 1024)}K" if ctx else "?"
            tool_text = "工具" if m.get("tools") else "无工具"
            model_items.append(
                {"title": f"{m['name']}  ·  {m['id']}  [{ctx_text}/{tool_text}]", "value": m["id"]}
            )
        if not model_items:
            model_items = [{"title": "（暂无数据，请先填写 API Key 后保存，再重新打开）", "value": ""}]

        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "auto_apply", "label": "保存时自动填充"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "persist_env", "label": "写入 app.env 持久化"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "api_key",
                                            "label": "OpenRouter API Key",
                                            "type": "password",
                                            "placeholder": "在 https://openrouter.ai/keys 创建，调用免费模型同样需要",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enable_agent", "label": "同时启用智能助手"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "base_url",
                                            "label": "API 地址 (LLM_BASE_URL)",
                                            "placeholder": DEFAULT_BASE_URL,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "list_url",
                                            "label": "模型目录接口",
                                            "placeholder": DEFAULT_LIST_URL,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "model",
                                            "label": "免费模型 (LLM_MODEL)",
                                            "items": model_items,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "provider",
                                            "label": "提供商 (LLM_PROVIDER)",
                                            "items": [
                                                {"title": "openrouter", "value": "openrouter"},
                                                {"title": "openai (兼容模式)", "value": "openai"},
                                                {"title": "custom", "value": "custom"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "protocol",
                                            "label": "接口协议",
                                            "items": [
                                                {
                                                    "title": "chat_completions",
                                                    "value": "chat_completions",
                                                },
                                                {"title": "responses", "value": "responses"},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "refresh_hours",
                                            "label": "自动刷新间隔(小时)",
                                            "type": "number",
                                            "placeholder": "12，填 0 关闭",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "use_proxy", "label": "走系统代理"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "only_tool_models",
                                            "label": "仅显示支持工具调用",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "site_url",
                                            "label": "应用地址 (可选，OpenRouter 归属头)",
                                            "placeholder": "https://your-site.example.com",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "site_name",
                                            "label": "应用名称 (可选，X-Title)",
                                            "placeholder": "MoviePilot",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "说明：OpenRouter 调用免费模型同样必须携带 API Key（免费额度按账号计，"
                                                "未充值约 50 次/天，累计充值 ≥10 美元后约 1000 次/天）。"
                                                "智能助手依赖 tool calling，建议保持「仅显示支持工具调用」开启。"
                                                "勾选「写入 app.env 持久化」后，配置在重启后仍生效；"
                                                "若同名配置已由 Docker 环境变量注入，环境变量优先级更高。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

        default_settings = {
            "enabled": self._enabled,
            "auto_apply": self._auto_apply,
            "persist_env": self._persist_env,
            "enable_agent": self._enable_agent,
            "use_proxy": self._use_proxy,
            "only_tool_models": self._only_tool_models,
            "api_key": self._api_key,
            "base_url": self._base_url,
            "list_url": self._list_url,
            "provider": self._provider,
            "protocol": self._protocol,
            "model": self._model,
            "refresh_hours": self._refresh_hours,
            "site_url": self._site_url,
            "site_name": self._site_name,
        }
        return form, default_settings

    def get_page(self) -> List[dict]:
        models = self._models_cache or []
        last = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_refresh))
            if self._last_refresh
            else "从未刷新"
        )
        shown_key = (
            f"{self._api_key[:6]}***"
            if len(self._api_key) > 6
            else ("已填写" if self._api_key else "未填写")
        )
        tool_count = sum(1 for m in models if m.get("tools"))
        model_text = (
            "、".join(m["id"] for m in models)
            if models
            else "暂无（请填写 API Key 后打开配置页刷新）"
        )

        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if self._enabled else "warning",
                    "variant": "tonal",
                    "title": "运行状态",
                    "text": (
                        f"插件：{'已启用' if self._enabled else '未启用'}　"
                        f"上次刷新：{last}　免费模型：{len(models)} 个（支持工具调用 {tool_count} 个）"
                    ),
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "title": "当前智能助手配置",
                    "text": (
                        f"LLM_PROVIDER={self._provider}　"
                        f"LLM_MODEL={self._model or '未选择'}　"
                        f"LLM_BASE_URL={self._base_url}　"
                        f"API Key={shown_key}"
                    ),
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "secondary",
                    "variant": "tonal",
                    "title": "可用免费模型",
                    "text": model_text,
                },
            },
        ]

    # ======================= 远程命令 =======================

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []
