"""
MoviePilot 插件：OpenRouter 免费模型助手 (OpenRouterHelper)

功能：
    1. 自动从 OpenRouter 拉取模型目录，识别其中的免费模型
       （pricing.prompt == 0 且 pricing.completion == 0，排除音频生成等非对话模型）。
    2. 将选定的免费模型一键填充到 MoviePilot「智能助手 / LLM」配置：
           LLM_PROVIDER / LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / LLM_API_PROTOCOL
       可选同时打开 AI_AGENT_ENABLE。
    3. 标注每个免费模型是否支持 tool calling（智能助手靠它调工具，强烈建议只选支持的）。
    4. 【测速】对每个免费模型实发一条最小对话请求，测出真实响应延迟并排序。
    5. 【可用性探测】同一批请求顺带判定模型当前能否调用，分类为：
       ok / 无 Key / 鉴权失败(401) / 额度耗尽(429) / 模型不存在(404) /
       参数不支持(400) / 超时 / 网络异常 / 其他错误。
       探测结果会标注到下拉列表上（如 `[977K/工具/320ms]` 或 `[不可用]`），
       并支持「自动切换到最快可用模型」。
    6. 支持写入 app.env 持久化（重启后仍生效），支持定时刷新目录与定时测速。

关于探测代价（重要）：
    一次探测 = 一次真实对话请求，会消耗 OpenRouter 免费额度（未充值约 50 次/天）。
    因此：自动测速默认关闭；探测并发默认 4；一旦发现鉴权失败或额度耗尽会立即熔断
    并停止剩余模型的探测，避免白烧配额。

实测边界（2026-10）：
    - GET https://openrouter.ai/api/v1/models 无需鉴权即可拉取（458 个模型，其中 19 个零价、16 个支持 tools）。
    - 调用 :free 模型必须携带 OpenRouter API Key（https://openrouter.ai/keys），否则返回 401。
      OpenRouter 免费额度随账号余额变化：未充值约 50 次/天，累计充值 ≥10 美元后约 1000 次/天。
    - 免费模型 id 形如 `vendor/model:free`，但 `openrouter/free`、`inclusionai/ling-3.1-flash`
      这类零价模型不带 :free 后缀，所以本插件按「零价」判定而不是按后缀。

依赖：requests（MoviePilot 自带）
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.log import logger

# --- 宿主基类：兼容 v2(app.plugins) 与 v3(app.sdk.plugin) ---
try:
    from app.plugins import _PluginBase
except ImportError:  # pragma: no cover - 新版本归位到 SDK
    from app.sdk.plugin import _PluginBase

# --- 运行配置对象：v2 用 app.core.config，v3 优先 app.sdk.config ---
try:
    from app.core.config import settings
except ImportError:  # pragma: no cover
    try:
        from app.sdk.config import settings
    except ImportError:  # pragma: no cover
        from app.runtime.config import settings

# --- 模块管理器：改配置后热重载（v2: app.core.module；v3: app.sdk.plugin）---
try:
    from app.core.module import ModuleManager
except ImportError:  # pragma: no cover
    try:
        from app.sdk.plugin import ModuleManager
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

# ---------- 可用性探测状态 ----------
# ok       : 成功返回内容，可正常调用
# no_key   : 未填写 API Key，未发起请求
# auth     : 401，Key 无效或缺失
# quota    : 429，免费额度耗尽/限流
# missing  : 404，模型不存在或已下架
# badreq   : 400/422，请求被拒（模型不支持该协议/参数等）
# timeout  : 请求超时
# network  : 连接层异常（DNS/代理/TLS）
# error    : 其他非 200
# skipped  : 因熔断未执行
PROBE_OK = "ok"
# 会触发熔断、立即停止剩余探测的状态（继续探测纯属浪费配额）
PROBE_FATAL = ("auth", "quota")
# 状态中文标签，用于界面展示
PROBE_LABEL = {
    "ok": "可用",
    "no_key": "无Key",
    "auth": "鉴权失败",
    "quota": "额度耗尽",
    "missing": "模型不存在",
    "badreq": "请求被拒",
    "timeout": "超时",
    "network": "网络异常",
    "error": "错误",
    "skipped": "已跳过",
}


class OpenRouterHelper(_PluginBase):
    # ---------- 插件元信息 ----------
    plugin_name = "OpenRouter 免费模型助手"
    plugin_desc = "自动获取 OpenRouter 免费模型，自动测速并检测可用性，一键填充到智能助手(LLM)配置。"
    plugin_icon = "openrouterhelper.png"
    plugin_version = "1.1.0"
    plugin_author = "MT1992"
    author_url = "https://github.com/MT1992"
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

    # ---------- 测速 / 可用性探测 ----------
    _probe_enabled: bool = False
    _probe_hours: int = 6
    _probe_concurrency: int = 4
    _probe_timeout: int = 45
    _probe_limit: int = 0
    _probe_prompt: str = "hi"
    _auto_switch: bool = False

    _models_cache: List[dict] = []
    _last_refresh: Optional[float] = None
    # {model_id: {"status","latency_ms","at","code","error"}}
    _probe_results: Dict[str, dict] = {}
    _last_probe: Optional[float] = None
    _probe_abort: Optional[threading.Event] = None
    _probe_lock = threading.Lock()

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

        # ---------- 测速 / 探测 ----------
        self._probe_enabled = bool(config.get("probe_enabled"))
        self._auto_switch = bool(config.get("auto_switch"))
        self._probe_prompt = str(config.get("probe_prompt") or "hi").strip() or "hi"
        self._probe_hours = self._as_int(config.get("probe_hours"), 6, 0, 24 * 30)
        self._probe_concurrency = self._as_int(config.get("probe_concurrency"), 4, 1, 16)
        self._probe_timeout = self._as_int(config.get("probe_timeout"), 45, 5, 300)
        self._probe_limit = self._as_int(config.get("probe_limit"), 0, 0, 999)
        self._refresh_hours = self._as_int(config.get("refresh_hours"), 12, 0, 24 * 30)

        # 载入本地缓存（避免启动即发网络请求）
        cache = self.get_data("models_cache") or {}
        self._models_cache = cache.get("models") or []
        self._last_refresh = cache.get("ts")

        probe_cache = self.get_data("probe_cache") or {}
        self._probe_results = probe_cache.get("results") or {}
        self._last_probe = probe_cache.get("ts")
        # 只保留当前仍在免费列表中的模型的探测结果
        if self._models_cache and self._probe_results:
            alive = {m["id"] for m in self._models_cache}
            self._probe_results = {
                k: v for k, v in self._probe_results.items() if k in alive
            }

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

    @staticmethod
    def _as_int(value: Any, default: int, low: int, high: int) -> int:
        """把配置值安全地转成 [low, high] 区间内的整数。"""
        try:
            num = int(float(value))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, num))

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

            # 目录变了，清掉已下架模型的探测记录，避免界面显示陈旧测速
            alive = {m["id"] for m in data}
            stale = [mid for mid in self._probe_results if mid not in alive]
            if stale:
                with self._probe_lock:
                    for mid in stale:
                        self._probe_results.pop(mid, None)
                self._save_probe_cache()
                logger.info(f"OpenRouterHelper 已清理 {len(stale)} 条失效测速记录")

        # 自动兜底：当前选中的模型已不在免费列表里（下架/转付费）
        if data and self._model and self._model not in {m["id"] for m in data}:
            logger.warn(
                f"OpenRouterHelper 当前选中的模型 {self._model} 已不在免费列表中，请重新选择"
            )
        return self._models_cache

    # ======================= 测速 / 可用性探测 =======================

    def _save_probe_cache(self):
        # 兜底：确保探测结果挂在实例上，避免污染类属性（init_plugin 未调用时）
        if "_probe_results" not in self.__dict__:
            self._probe_results = dict(self._probe_results or {})
        self.save_data(
            "probe_cache",
            {"results": dict(self._probe_results), "ts": self._last_probe},
        )

    def _probe_payload(self, model_id: str) -> dict:
        return {
            "model": model_id,
            "messages": [{"role": "user", "content": self._probe_prompt}],
            "max_tokens": 8,
            "temperature": 0,
        }

    @staticmethod
    def _classify(code: int, payload: Any) -> str:
        """把 HTTP 状态码归类成探测状态。"""
        if code == 200:
            return PROBE_OK
        if code == 401 or code == 403:
            return "auth"
        if code == 429:
            return "quota"
        if code == 404:
            return "missing"
        if code in (400, 422):
            return "badreq"
        return "error"

    @staticmethod
    def _error_text(payload: Any) -> str:
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or "")[:300]
            if isinstance(error, str):
                return error[:300]
            if payload.get("message"):
                return str(payload["message"])[:300]
        return str(payload)[:300]

    def _probe_one(self, model_id: str) -> dict:
        """对单个模型实发一条最小请求，返回 {status, latency_ms, code, error}。

        这里刻意不复用 api_test 的格式化逻辑：探测只需要机器可判定的状态，
        且必须在并发下保持无副作用。
        """
        abort = self._probe_abort
        if abort is not None and abort.is_set():
            return {"status": "skipped", "latency_ms": None, "code": None, "error": "已熔断"}

        if not self._api_key:
            # 没 Key 时不去打上游（必然是 401），省一次网络往返
            return {
                "status": "no_key",
                "latency_ms": None,
                "code": None,
                "error": "未填写 API Key",
            }

        started = time.time()
        try:
            resp = requests.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(json_body=True),
                json=self._probe_payload(model_id),
                proxies=self._proxies(),
                timeout=self._probe_timeout,
            )
        except requests.exceptions.Timeout:
            return {
                "status": "timeout",
                "latency_ms": round((time.time() - started) * 1000),
                "code": None,
                "error": f"超过 {self._probe_timeout}s 未响应",
            }
        except Exception as err:  # noqa: BLE001
            return {
                "status": "network",
                "latency_ms": round((time.time() - started) * 1000),
                "code": None,
                "error": str(err)[:300],
            }

        latency = round((time.time() - started) * 1000)
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            payload = {"raw": (resp.text or "")[:300]}

        status = self._classify(resp.status_code, payload)
        if status == PROBE_OK and not (payload.get("choices") if isinstance(payload, dict) else None):
            # 200 但没有 choices：算异常，不能算可用
            status = "error"

        return {
            "status": status,
            "latency_ms": latency,
            "code": resp.status_code,
            "error": "" if status == PROBE_OK else self._error_text(payload),
        }

    def _candidates(self, only_missing: bool = False) -> List[str]:
        """待探测的模型 id 列表。"""
        models = self._models_cache or []
        ids = [m["id"] for m in models]
        if only_missing:
            ids = [i for i in ids if i not in self._probe_results]
        if self._probe_limit > 0:
            ids = ids[: self._probe_limit]
        return ids

    def probe_models(
        self, model_ids: Optional[List[str]] = None, only_missing: bool = False
    ) -> Dict[str, Any]:
        """并发探测多个模型的延迟与可用性。"""
        ids = list(model_ids) if model_ids else self._candidates(only_missing=only_missing)
        if not ids:
            return {
                "success": False,
                "message": "没有需要探测的模型（列表为空，或已全部探测过）",
                "data": None,
            }

        # 每次探测都会消耗免费额度，先明确告知代价
        estimated = len(ids)
        workers = max(1, min(self._probe_concurrency, len(ids)))
        logger.info(
            f"OpenRouterHelper 开始探测 {estimated} 个模型"
            f"（并发 {workers}，超时 {self._probe_timeout}s，约消耗 {estimated} 次免费额度）"
        )

        self._probe_abort = threading.Event()
        abort = self._probe_abort
        started = time.time()
        results: Dict[str, dict] = {}
        aborted = False

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                fut_map = {pool.submit(self._probe_one, mid): mid for mid in ids}
                for fut in as_completed(fut_map):
                    mid = fut_map[fut]
                    try:
                        item = fut.result()
                    except Exception as err:  # noqa: BLE001
                        item = {
                            "status": "error",
                            "latency_ms": None,
                            "code": None,
                            "error": str(err)[:300],
                        }
                    item["at"] = time.time()
                    results[mid] = item

                    # 熔断：Key 无效 / 额度耗尽时，剩余探测没有意义
                    if item["status"] in PROBE_FATAL:
                        if not aborted:
                            aborted = True
                            abort.set()
                            logger.warn(
                                f"OpenRouterHelper 探测熔断（{PROBE_LABEL.get(item['status'])}），"
                                f"停止剩余 {len(ids) - len(results)} 个模型的探测以节省配额"
                            )
        finally:
            self._probe_abort = None

        with self._probe_lock:
            self._probe_results.update(results)
            self._last_probe = time.time()
        self._save_probe_cache()

        ok = {k: v for k, v in results.items() if v["status"] == PROBE_OK}
        latencies = [v["latency_ms"] for v in ok.values() if v.get("latency_ms") is not None]
        summary = {
            "total": len(results),
            "ok": len(ok),
            "failed": len(results) - len(ok),
            "skipped": sum(1 for v in results.values() if v["status"] == "skipped"),
            "aborted": aborted,
            "elapsed_ms": round((time.time() - started) * 1000),
            "fastest": self._fastest(),
        }
        if latencies:
            summary["avg_ms"] = round(sum(latencies) / len(latencies))

        logger.info(
            f"OpenRouterHelper 探测完成：可用 {summary['ok']}/{summary['total']}，"
            f"平均 {summary.get('avg_ms', '-')}ms，最快 {summary.get('fastest')}"
        )
        return {
            "success": bool(ok),
            "message": (
                f"探测完成：可用 {summary['ok']}/{summary['total']}"
                + (f"，最快 {summary['fastest']['id']}（{summary['fastest']['latency_ms']}ms）"
                   if summary.get("fastest") else "")
                + ("（已熔断，其余跳过）" if aborted else "")
            ),
            "data": {"results": results, "summary": summary},
        }

    def _pick_best(self, only_tools: bool = True) -> Optional[dict]:
        """在已探测结果里挑最快的可用模型。"""
        best = None
        for mid, item in self._probe_results.items():
            if item.get("status") != PROBE_OK or item.get("latency_ms") is None:
                continue
            if only_tools:
                meta = next((m for m in self._models_cache if m["id"] == mid), None)
                if meta is None or not meta.get("tools"):
                    continue
            if best is None or item["latency_ms"] < best["latency_ms"]:
                best = {"id": mid, "latency_ms": item["latency_ms"], "at": item.get("at")}
        return best

    def _fastest(self) -> Optional[dict]:
        best = self._pick_best()
        return {"id": best["id"], "latency_ms": best["latency_ms"]} if best else None

    def _persist_model_choice(self):
        """把自动切换后的模型选择写回插件配置，避免重启后回退到旧模型。"""
        updater = getattr(self, "update_config", None)
        if not callable(updater):
            logger.info("OpenRouterHelper 宿主不支持 update_config，模型选择仅本次运行生效")
            return
        try:
            config = self._current_config()
            config["model"] = self._model
            updater(config)
            logger.info(f"OpenRouterHelper 已持久化模型选择：{self._model}")
        except Exception as err:  # noqa: BLE001
            logger.warn(f"OpenRouterHelper 持久化模型选择失败：{err}")

    def probe_and_maybe_switch(self) -> Dict[str, Any]:
        """探测 + 可选的自动切换到最快可用模型。"""
        report = self.probe_models()
        best = self._pick_best()
        if not best:
            return report

        current = self._probe_results.get(self._model) or {}
        reason = None
        if self._model != best["id"]:
            if current.get("status") != PROBE_OK:
                reason = "当前模型不可用"
            elif self._auto_switch:
                reason = "自动切换到最快可用模型"

        if reason:
            old = self._model
            self._model = best["id"]
            self._persist_model_choice()
            try:
                self.apply_to_agent()
                logger.info(
                    f"OpenRouterHelper {reason}：{old or '未选择'} -> {best['id']}"
                    f"（{best['latency_ms']}ms）"
                )
                report["switched"] = {"from": old, "to": best["id"], "reason": reason}
            except Exception as err:  # noqa: BLE001
                logger.error(f"OpenRouterHelper 自动切换模型失败：{err}")
        return report

    def _probe_badge(self, model_id: str) -> Optional[str]:
        """给下拉标题用的探测标注；未测速返回 None（不占位）。"""
        item = self._probe_results.get(model_id)
        if not item:
            return None
        status = item.get("status")
        if status == PROBE_OK:
            return f"{item.get('latency_ms')}ms"
        return PROBE_LABEL.get(status, status or "未知")

    def ranked_models(self) -> List[dict]:
        """按「可用优先 → 延迟升序 → 上下文降序」排列模型。

        排序意图：能用的排在前面，其中越快的越靠前；没测过的按上下文大小排；
        探测确认不可用的排到最后，方便一眼看出该选哪个。
        """
        def sort_key(item: dict):
            mid = item["id"]
            probe = self._probe_results.get(mid) or {}
            status = probe.get("status")
            if status == PROBE_OK:
                tier = 0
                latency = probe.get("latency_ms")
                latency = latency if latency is not None else 10**9
            elif status is None:
                tier = 1
                latency = 0
            else:
                tier = 2
                latency = 0
            return (tier, latency, -(item.get("context") or 0), mid)

        ranked = []
        for m in sorted(self._models_cache or [], key=sort_key):
            probe = self._probe_results.get(m["id"]) or {}
            item = dict(m)
            item["status"] = probe.get("status")
            item["latency_ms"] = probe.get("latency_ms")
            item["status_label"] = (
                PROBE_LABEL.get(probe["status"], probe["status"]) if probe.get("status") else "未测速"
            )
            item["error"] = probe.get("error") or ""
            item["tested_at"] = probe.get("at")
            ranked.append(item)
        return ranked

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
        """改完配置后热重载模块，让智能助手立即用上新模型。

        宿主各版本的模块管理器入口与方法名并不统一（v2 为
        app.core.module.ModuleManager.load_modules，v3 已不再导出该方法），
        所以这里逐级探测；不支持时安静跳过 —— 配置已写入 app.env，
        重启容器后同样生效。
        """
        if not ModuleManager:
            logger.info("OpenRouterHelper 未找到模块管理器，配置将在下次重启后生效")
            return
        try:
            manager = ModuleManager()
            stop = getattr(manager, "stop", None)
            load = getattr(manager, "load_modules", None) or getattr(manager, "reload", None)
            if stop:
                stop()
            if load:
                load()
            else:
                logger.info("OpenRouterHelper 当前宿主不支持热重载，配置将在下次重启后生效")
        except Exception as err:  # noqa: BLE001
            logger.warn(f"OpenRouterHelper 模块重载失败（配置已写入，重启后生效）：{err}")

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

    def _current_config(self) -> Dict[str, Any]:
        """当前配置快照（与 get_form 的默认值同构），用于 update_config 持久化。"""
        return {
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
            "probe_enabled": self._probe_enabled,
            "probe_hours": self._probe_hours,
            "probe_concurrency": self._probe_concurrency,
            "probe_timeout": self._probe_timeout,
            "probe_limit": self._probe_limit,
            "probe_prompt": self._probe_prompt,
            "auto_switch": self._auto_switch,
        }

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
            {
                "path": "/probe",
                "endpoint": self.api_probe,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "测速并检测可用性",
                "description": (
                    "并发探测免费模型的响应延迟与可调用性，返回每个模型的状态；"
                    "命中鉴权失败/额度耗尽会自动熔断。注意：会消耗免费额度"
                ),
            },
            {
                "path": "/probe_model",
                "endpoint": self.api_probe_model,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "探测单个模型",
                "description": "只对指定 model 参数做一次测速与可用性检测",
            },
            {
                "path": "/ranking",
                "endpoint": self.api_ranking,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "测速排行",
                "description": "按「可用优先 + 延迟升序」返回模型排行",
            },
        ]

    def api_models(self) -> Dict[str, Any]:
        ranked = self.ranked_models()
        probed = [m for m in ranked if m.get("status")]
        return {
            "success": True,
            "message": "ok",
            "data": {
                "models": ranked,
                "last_refresh": self._last_refresh,
                "last_probe": self._last_probe,
                "count": len(ranked),
                "probed": len(probed),
                "available": sum(1 for m in probed if m.get("status") == PROBE_OK),
                "fastest": self._fastest(),
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
                timeout=self._probe_timeout,
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

    def api_probe(self) -> Dict[str, Any]:
        """探测全部（或前 N 个）免费模型的延迟与可用性。"""
        if not self._models_cache:
            self.refresh_models()
        return self.probe_and_maybe_switch()

    def api_probe_model(self, model: Optional[str] = None) -> Dict[str, Any]:
        """探测单个模型。"""
        target = str(model or self._model or "").strip()
        if not target:
            return {"success": False, "message": "请指定 model 参数或先选择模型", "data": None}
        report = self.probe_models(model_ids=[target])
        item = (report.get("data") or {}).get("results", {}).get(target) or {}
        ok = item.get("status") == PROBE_OK
        return {
            "success": ok,
            "message": (
                f"{target} 可用，延迟 {item.get('latency_ms')}ms"
                if ok
                else f"{target} 不可用：{PROBE_LABEL.get(item.get('status'), item.get('status'))}"
                     f"{' - ' + item['error'] if item.get('error') else ''}"
            ),
            "data": item,
        }

    def api_ranking(self) -> Dict[str, Any]:
        """按可用性与延迟排出模型榜单。"""
        return {
            "success": True,
            "message": "ok",
            "data": {
                "ranked": self.ranked_models(),
                "last_probe": self._last_probe,
                "probed": len(self._probe_results),
                "models": len(self._models_cache),
            },
        }

    # ======================= 定时服务 =======================

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not CronTrigger:
            return []
        services: List[Dict[str, Any]] = []

        # 1) 定时刷新模型目录
        if self._refresh_hours > 0:
            if self._refresh_hours >= 24:
                crontab = "0 3 * * *"
            else:
                crontab = f"0 */{max(1, self._refresh_hours)} * * *"
            services.append(
                {
                    "id": "OpenRouterHelper.Refresh",
                    "name": "OpenRouter 免费模型自动刷新",
                    "trigger": CronTrigger.from_crontab(crontab),
                    "func": self.service_refresh,
                    "kwargs": {},
                }
            )

        # 2) 定时测速（与刷新错开 30 分钟，避免同时打上游）
        if self._probe_enabled and self._probe_hours > 0:
            if self._probe_hours >= 24:
                probe_cron = "30 3 * * *"
            else:
                probe_cron = f"30 */{max(1, self._probe_hours)} * * *"
            services.append(
                {
                    "id": "OpenRouterHelper.Probe",
                    "name": "OpenRouter 免费模型自动测速",
                    "trigger": CronTrigger.from_crontab(probe_cron),
                    "func": self.service_probe,
                    "kwargs": {},
                }
            )
        return services

    def service_probe(self):
        """定时探测：先确保目录不过期，再测速，必要时切换模型。"""
        if not self._models_cache:
            self.refresh_models(force=True)
        try:
            self.probe_and_maybe_switch()
        except Exception as err:  # noqa: BLE001
            logger.error(f"OpenRouterHelper 定时测速失败：{err}")

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
        for m in self.ranked_models():
            ctx = m.get("context") or 0
            ctx_text = f"{round(ctx / 1024)}K" if ctx else "?"
            tool_text = "工具" if m.get("tools") else "无工具"
            parts = [ctx_text, tool_text]
            badge = self._probe_badge(m["id"])
            if badge:
                parts.append(badge)
            title = f"{m['name']}  ·  {m['id']}  [{'/'.join(parts)}]"
            if m.get("status") and m["status"] != PROBE_OK:
                title = f"✗ {title}"
            elif m.get("status") == PROBE_OK:
                title = f"✓ {title}"
            model_items.append({"title": title, "value": m["id"]})
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
                                            "label": "目录刷新间隔(小时)",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "probe_hours",
                                            "label": "自动测速间隔(小时)",
                                            "type": "number",
                                            "placeholder": "6，填 0 关闭",
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
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "probe_enabled",
                                            "label": "启用自动测速与可用性检测",
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
                                        "props": {
                                            "model": "auto_switch",
                                            "label": "自动切换到最快可用模型",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "probe_concurrency",
                                            "label": "测速并发数",
                                            "type": "number",
                                            "placeholder": "4（1-16）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "probe_timeout",
                                            "label": "单次探测超时(秒)",
                                            "type": "number",
                                            "placeholder": "45",
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
                                            "model": "probe_limit",
                                            "label": "每次最多探测模型数",
                                            "type": "number",
                                            "placeholder": "0 = 全部（会消耗等量免费额度）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 9},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "probe_prompt",
                                            "label": "探测问题（越短越省 token）",
                                            "placeholder": "hi",
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
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": (
                                                "测速说明：一次测速 = 向每个模型实发一条真实对话请求，"
                                                "会按模型个数消耗 OpenRouter 免费额度（例如 16 个模型即消耗 16 次）。"
                                                "因此默认不开启自动测速；首次建议先点「开始测速」跑一轮，"
                                                "结果会缓存并标注到上面的模型下拉框（✓ 可用 / ✗ 不可用 / 延迟 ms）。"
                                                "探测到鉴权失败(401)或额度耗尽(429)会立即熔断，停止剩余模型以免白烧配额。"
                                                "「自动切换到最快可用模型」会在测速后把 LLM_MODEL 换成延迟最低的可用模型，"
                                                "并写回插件配置。"
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

        return form, self._current_config()

    def get_page(self) -> List[dict]:
        models = self._models_cache or []
        last = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_refresh))
            if self._last_refresh
            else "从未刷新"
        )
        last_probe = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_probe))
            if self._last_probe
            else "从未测速"
        )
        shown_key = (
            f"{self._api_key[:6]}***"
            if len(self._api_key) > 6
            else ("已填写" if self._api_key else "未填写")
        )
        tool_count = sum(1 for m in models if m.get("tools"))

        ranked = self.ranked_models()
        ok_models = [m for m in ranked if m.get("status") == PROBE_OK]
        bad_models = [m for m in ranked if m.get("status") and m["status"] != PROBE_OK]
        untested = [m for m in ranked if not m.get("status")]

        if ok_models:
            latency_list = [m["latency_ms"] for m in ok_models if m.get("latency_ms") is not None]
            avg = f"，平均 {round(sum(latency_list) / len(latency_list))}ms" if latency_list else ""
            ranking_text = "　".join(
                f"{i}. {m['id']} ({m['latency_ms']}ms)"
                for i, m in enumerate(ok_models[:8], 1)
            ) + f"　共 {len(ok_models)} 个可用{avg}"
        else:
            ranking_text = "暂无可用记录，可调用 /probe 接口或点配置页「开始测速」"

        bad_text = (
            "　".join(f"{m['id']}（{m['status_label']}）" for m in bad_models[:10])
            if bad_models
            else "无"
        )

        alerts = [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if self._enabled else "warning",
                    "variant": "tonal",
                    "title": "运行状态",
                    "text": (
                        f"插件：{'已启用' if self._enabled else '未启用'}　"
                        f"上次刷新：{last}　免费模型：{len(models)} 个（支持工具调用 {tool_count} 个）　"
                        f"上次测速：{last_probe}　已测：{len(self._probe_results)} 个"
                        f"（可用 {len(ok_models)}／未测 {len(untested)}／异常 {len(bad_models)}）"
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
                        f"API Key={shown_key}　"
                        f"自动测速：{'开' if self._probe_enabled else '关'}"
                        f"（每 {self._probe_hours}h）　"
                        f"自动切换：{'开' if self._auto_switch else '关'}"
                    ),
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if ok_models else "secondary",
                    "variant": "tonal",
                    "title": "测速排行（可用模型，按延迟升序）",
                    "text": ranking_text,
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "error" if bad_models else "secondary",
                    "variant": "tonal",
                    "title": "检测到不可用的模型",
                    "text": bad_text,
                },
            },
        ]
        return alerts

    # ======================= 远程命令 =======================

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []
