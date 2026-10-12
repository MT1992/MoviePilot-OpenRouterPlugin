"""
OpenRouterHelper 离线自检脚本（开发用，不属于插件本体）

用桩模块模拟 MoviePilot 宿主（app.log / app.plugins / app.core.config / app.core.module），
在不装 MoviePilot 的情况下验证插件核心逻辑：
    - 免费模型判定（零价 + 文本输出）与 tool calling 标注
    - 上游 /api/v1/models 拉取与过滤（用真实抓取的样本离线回放）
    - 填充智能助手：setattr(settings) + 模块热重载 + app.env 持久化 + 幂等
    - get_form / get_page 结构
    - /test 接口的成功、401、429 分支
    - 模块管理器三种降级路径（无管理器 / 无 load_modules / 管理器报错）
    - 测速与可用性探测：状态分类、缺 Key 短路、超时/网络异常、
      并发全量探测与延迟排序、401/429 熔断、自动切换最快可用模型、
      定时测速任务注册、探测结果持久化与下架清理

运行：
    "C:/Users/wangh/.workbuddy/binaries/python/envs/default/Scripts/python.exe" dev/selftest.py
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUBS = ROOT / "dev" / "_stubs"
PLUGIN_DIR = ROOT / "plugins.v2"
SAMPLE = ROOT / "dev" / "or_models.sample.json"

# 让 `import app.*` 命中桩，让 `import openrouterhelper` 命中插件目录
sys.path.insert(0, str(STUBS))
sys.path.insert(0, str(PLUGIN_DIR))

import openrouterhelper  # noqa: E402
from openrouterhelper import OpenRouterHelper  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


SAMPLE_PAYLOAD = json.loads(SAMPLE.read_text(encoding="utf-8"))


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)[:300]

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


calls = {"get": 0, "post": 0}


def fake_get(url, **kwargs):
    calls["get"] += 1
    return _FakeResp(SAMPLE_PAYLOAD)


def fake_post_ok(url, **kwargs):
    calls["post"] += 1
    calls["payload"] = kwargs.get("json")
    calls["headers"] = kwargs.get("headers")
    return _FakeResp(
        {"id": "gen-1", "choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    )


def fake_post_401(url, **kwargs):
    calls["post"] += 1
    return _FakeResp({"error": {"code": 401, "message": "No auth credentials found"}}, status=401)


def fake_post_429(url, **kwargs):
    calls["post"] += 1
    return _FakeResp(
        {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day"}}, status=429
    )


def walk_for_model_select(node, found):
    if isinstance(node, dict):
        props = node.get("props") or {}
        if props.get("model") == "model" and "items" in props:
            found.append(props["items"])
        for v in node.values():
            walk_for_model_select(v, found)
    elif isinstance(node, list):
        for v in node:
            walk_for_model_select(v, found)


def main():
    print("\n=== 1. 免费模型判定（基于真实样本）===")
    all_models = SAMPLE_PAYLOAD["data"]
    check("样本包含 458 个模型", len(all_models) == 458, str(len(all_models)))

    free = [m for m in all_models if OpenRouterHelper._is_free(m)]
    check("零价且输出文本的模型 = 17", len(free) == 17, f"实际 {len(free)}：{[m['id'] for m in free]}")
    ids = {m["id"] for m in free}
    check("排除了音频生成模型 lyria", not any("lyria" in i for i in ids))
    check("含 openrouter/free", "openrouter/free" in ids)
    check("含 inclusionai/ling-3.1-flash（零价但不带 :free）", "inclusionai/ling-3.1-flash" in ids)
    check("付费模型不入选", "openai/gpt-5" not in ids and "anthropic/claude-sonnet-4" not in ids)

    print("\n=== 2. init_plugin 读取配置 ===")
    plugin = OpenRouterHelper()
    plugin.init_plugin(
        {
            "enabled": True,
            "auto_apply": False,
            "persist_env": True,
            "enable_agent": True,
            "api_key": "sk-or-v1-testkey1234567890",
            "base_url": "https://openrouter.ai/api/v1/",
            "list_url": "https://openrouter.ai/api/v1/models",
            "provider": "openrouter",
            "protocol": "chat_completions",
            "model": "nvidia/nemotron-3.5-lightning:free",
            "refresh_hours": 12,
            "only_tool_models": True,
        }
    )
    check("enabled 生效", plugin.get_state() is True)
    check("provider 生效", plugin._provider == "openrouter")
    check("base_url 去掉末尾斜杠", plugin._base_url == "https://openrouter.ai/api/v1")
    check("只保留支持工具调用", plugin._only_tool_models is True)

    openrouterhelper.requests.get = fake_get
    openrouterhelper.requests.post = fake_post_ok

    print("\n=== 3. 拉取并过滤（mock 上游目录）===")
    models = plugin.refresh_models(force=True)
    got = [m["id"] for m in models]
    check("默认只留支持 tools 的 16 个", len(models) == 16, f"实际 {len(models)}")
    check("全部标记 tools=True", all(m["tools"] for m in models))
    check("排除了无工具的 content-safety", "nvidia/nemotron-3.5-content-safety:free" not in got)
    check("含 nemotron-3.5-lightning:free", "nvidia/nemotron-3.5-lightning:free" in got)
    check("按上下文长度降序排列", (models[0].get("context") or 0) >= (models[-1].get("context") or 0))

    plugin._only_tool_models = False
    models_all = plugin.refresh_models(force=True)
    check("关闭开关后为 17 个", len(models_all) == 17, f"实际 {len(models_all)}")
    plugin._only_tool_models = True
    plugin.refresh_models(force=True)

    print("\n=== 4. 填充智能助手（settings + 热重载 + app.env）===")
    from app.core.config import settings
    from app.core.module import ModuleManager
    from dotenv import load_dotenv

    # 故意删掉配置目录，验证 _write_env 会自行创建
    if settings.CONFIG_PATH.exists():
        shutil.rmtree(settings.CONFIG_PATH)

    plugin.apply_to_agent()
    check("LLM_PROVIDER 写入", settings.LLM_PROVIDER == "openrouter")
    check("LLM_MODEL 写入", settings.LLM_MODEL == "nvidia/nemotron-3.5-lightning:free")
    check("LLM_BASE_URL 写入", settings.LLM_BASE_URL == "https://openrouter.ai/api/v1")
    check("LLM_API_KEY 写入", settings.LLM_API_KEY == "sk-or-v1-testkey1234567890")
    check("AI_AGENT_ENABLE 打开", settings.AI_AGENT_ENABLE is True)
    check(
        "ModuleManager 已 stop+load",
        ModuleManager.stopped == 1 and ModuleManager.loaded == 1,
        f"stop={ModuleManager.stopped} load={ModuleManager.loaded}",
    )

    env_file = settings.CONFIG_PATH / "app.env"
    check("app.env 目录被自动创建", env_file.exists())
    load_dotenv(env_file, override=True)
    check(
        "app.env 持久化 LLM_MODEL",
        os.getenv("LLM_MODEL") == "nvidia/nemotron-3.5-lightning:free",
        repr(os.getenv("LLM_MODEL")),
    )
    check("app.env 持久化 LLM_PROVIDER", os.getenv("LLM_PROVIDER") == "openrouter")

    print("\n=== 5. 幂等：无变化不重复热重载 ===")
    before = (ModuleManager.stopped, ModuleManager.loaded)
    plugin.apply_to_agent()
    after = (ModuleManager.stopped, ModuleManager.loaded)
    check("配置未变时不再次重载模块", before == after, f"{before} -> {after}")

    print("\n=== 6. get_form / get_page 结构 ===")
    form, defaults = plugin.get_form()
    check("get_form 返回二元组", isinstance(form, list) and isinstance(defaults, dict))
    check("表单根为 VForm", form[0].get("component") == "VForm")
    check("默认值含 model 键", defaults.get("model") == "nvidia/nemotron-3.5-lightning:free")

    found = []
    walk_for_model_select(form, found)
    items = found[0] if found else []
    check("模型下拉注入 16 项", len(items) == 16, str(len(items)))
    check(
        "下拉标题含上下文与工具标注",
        bool(items) and "[1024K/工具]" in items[0]["title"],
        items[0]["title"] if items else "",
    )

    page = plugin.get_page()
    check("get_page 返回列表", isinstance(page, list) and len(page) >= 3)
    check(
        "详情页含测速排行告警",
        any("测速排行" in (p.get("props") or {}).get("title", "") for p in page),
    )
    check(
        "详情页含不可用模型告警",
        any("不可用" in (p.get("props") or {}).get("title", "") for p in page),
    )

    print("\n=== 7. API 端点 ===")
    r = plugin.api_models()
    check("api_models 成功", r["success"] and r["data"]["count"] == 16, str(r["data"]["count"]))

    r = plugin.api_apply()
    check("api_apply 成功", r["success"] is True)

    r = plugin.api_test()
    check("api_test 成功分支", r["success"] is True and "ok" in r["message"], r["message"])
    check(
        "请求带了 Bearer 与 Content-Type",
        calls["headers"].get("Authorization", "").startswith("Bearer ")
        and calls["headers"].get("Content-Type") == "application/json",
    )

    openrouterhelper.requests.post = fake_post_401
    r = plugin.api_test()
    check("api_test 提示 401 需填 Key", r["success"] is False and "API Key" in r["message"], r["message"])

    openrouterhelper.requests.post = fake_post_429
    r = plugin.api_test()
    check(
        "api_test 提示 429 免费额度用尽",
        r["success"] is False and "免费额度" in r["message"],
        r["message"],
    )
    openrouterhelper.requests.post = fake_post_ok

    print("\n=== 8. 定时服务注册 ===")
    services = plugin.get_service()
    check("注册了定时任务", len(services) == 1 and services[0]["id"] == "OpenRouterHelper.Refresh")
    plugin._refresh_hours = 0
    check("间隔为 0 时不注册", plugin.get_service() == [])
    plugin._refresh_hours = 24
    check(">=24 小时注册每日任务", len(plugin.get_service()) == 1)
    plugin._refresh_hours = 12

    print("\n=== 9. 下架/转付费兜底 ===")
    plugin._model = "vendor/removed-model:free"
    plugin.refresh_models(force=True)
    check("选中模型不在免费列表时给出提示（不崩溃）", True)

    print("\n=== 10. 模块管理器降级（v3 无 app/core/module.py 的场景）===")

    real_mm = openrouterhelper.ModuleManager

    # 10.1 宿主取不到模块管理器（v3：app.core.module 已删除，app.sdk.plugin 也没有时）
    openrouterhelper.ModuleManager = None
    try:
        plugin._reload_modules()
        check("无模块管理器时安静跳过（不抛异常）", True)
    except Exception as err:  # noqa: BLE001
        check("无模块管理器时安静跳过（不抛异常）", False, repr(err))

    # 10.2 管理器存在但只有 stop()，没有 load_modules()/reload()
    class _PartialManager:
        stopped = 0

        def stop(self):
            _PartialManager.stopped += 1

    openrouterhelper.ModuleManager = _PartialManager
    try:
        plugin._reload_modules()
        check(
            "管理器缺 load_modules 时不抛异常，只跳过加载",
            _PartialManager.stopped == 1,
            f"stop 调用 {_PartialManager.stopped} 次",
        )
    except Exception as err:  # noqa: BLE001
        check("管理器缺 load_modules 时不抛异常，只跳过加载", False, repr(err))

    # 10.3 管理器自身报错时必须被吞掉，不能冒泡打断 apply_to_agent
    class _BrokenManager:
        def __init__(self):
            raise RuntimeError("boom")

    openrouterhelper.ModuleManager = _BrokenManager
    try:
        plugin._reload_modules()
        check("管理器初始化失败被捕获（配置不受影响）", True)
    except Exception as err:  # noqa: BLE001
        check("管理器初始化失败被捕获（配置不受影响）", False, repr(err))

    openrouterhelper.ModuleManager = real_mm

    print("\n=== 11. 测速与可用性探测 ===")
    ids = [m["id"] for m in plugin._models_cache]
    m_a, m_b = ids[0], ids[1]

    # 11.1 探测请求体
    payload = plugin._probe_payload("test/model:free")
    check(
        "探测请求体最小化（model/messages/max_tokens/temperature）",
        payload["model"] == "test/model:free"
        and payload["messages"] == [{"role": "user", "content": "hi"}]
        and payload["max_tokens"] == 8
        and payload["temperature"] == 0,
        json.dumps(payload, ensure_ascii=False),
    )

    # 11.2 状态分类表
    cls_map = {
        200: "ok",
        401: "auth",
        403: "auth",
        429: "quota",
        404: "missing",
        400: "badreq",
        422: "badreq",
        500: "error",
        502: "error",
    }
    bad_cls = {c: OpenRouterHelper._classify(c, {}) for c, want in cls_map.items()
               if OpenRouterHelper._classify(c, {}) != want}
    check("HTTP 状态码分类正确", not bad_cls, str(bad_cls))

    # 11.3 单模型探测：成功
    openrouterhelper.requests.post = fake_post_ok
    item = plugin._probe_one(m_a)
    check("探测成功状态为 ok", item["status"] == "ok", item["status"])
    check(
        "返回延迟为整数毫秒",
        isinstance(item["latency_ms"], int) and item["latency_ms"] >= 0,
        repr(item["latency_ms"]),
    )
    check("记录 HTTP 200", item["code"] == 200)


    # 11.4 200 但无 choices 不算可用
    def fake_post_empty(url, **kwargs):
        return _FakeResp({"id": "x", "choices": []})

    openrouterhelper.requests.post = fake_post_empty
    check("200 但无 choices 判为 error", plugin._probe_one(m_a)["status"] == "error")

    # 11.5 缺 Key 时不打上游
    saved_key = plugin._api_key
    plugin._api_key = ""
    before_posts = calls["post"]
    item = plugin._probe_one(m_a)
    check("无 Key 时状态为 no_key", item["status"] == "no_key", item["status"])
    check("无 Key 时不发起网络请求（省一次往返）", calls["post"] == before_posts)
    plugin._api_key = saved_key

    # 11.6 超时 / 网络异常
    def fake_post_timeout(url, **kwargs):
        raise openrouterhelper.requests.exceptions.Timeout("timed out")

    def fake_post_conn(url, **kwargs):
        raise ConnectionError("dns failure")

    openrouterhelper.requests.post = fake_post_timeout
    check("超时归类为 timeout", plugin._probe_one(m_a)["status"] == "timeout")
    openrouterhelper.requests.post = fake_post_conn
    check("连接异常归类为 network", plugin._probe_one(m_a)["status"] == "network")

    # 11.7 全量并发探测 + 排行
    def make_post_latency(delay_map):
        def _post(url, **kwargs):
            calls["post"] += 1
            model = (kwargs.get("json") or {}).get("model")
            time.sleep(delay_map.get(model, 0.005))
            return _FakeResp(
                {"id": "gen", "choices": [{"message": {"role": "assistant", "content": "hi"}}]}
            )
        return _post

    plugin._probe_results = {}
    plugin._probe_concurrency = 4
    plugin._probe_timeout = 45
    delay_map = {mid: (i % 4) * 0.04 for i, mid in enumerate(ids)}
    openrouterhelper.requests.post = make_post_latency(delay_map)

    report = plugin.probe_models()
    summary = report["data"]["summary"]
    check("探测覆盖全部 16 个模型", summary["total"] == 16, str(summary))
    check("全部判定为可用", summary["ok"] == 16, str(summary))
    check("未触发熔断", summary["aborted"] is False)
    check("统计平均延迟", isinstance(summary.get("avg_ms"), int), str(summary.get("avg_ms")))
    check(
        "给出最快模型",
        bool(summary.get("fastest")) and summary["fastest"]["id"] in ids,
        str(summary.get("fastest")),
    )

    ranked = plugin.ranked_models()
    check("排行第一为可用模型", ranked[0].get("status") == "ok", str(ranked[0].get("status")))
    lats = [m["latency_ms"] for m in ranked if m.get("status") == "ok"]
    check("可用模型按延迟升序排列", lats == sorted(lats), str(lats))
    check(
        "排行项带状态中文标签",
        ranked[0]["status_label"] == "可用",
        ranked[0]["status_label"],
    )

    # 11.8 熔断：串行 + 全部 401，应立刻停止剩余探测
    plugin._probe_results = {}
    plugin._probe_concurrency = 1
    openrouterhelper.requests.post = fake_post_401
    report = plugin.probe_models()
    summary = report["data"]["summary"]
    check("鉴权失败触发熔断", summary["aborted"] is True)
    check("熔断后放弃剩余模型", summary["skipped"] > 0, f"skipped={summary['skipped']}")
    check("熔断批次里没有可用模型", summary["ok"] == 0)
    check(
        "结果中记录了 auth 状态",
        any(v["status"] == "auth" for v in report["data"]["results"].values()),
    )
    check("熔断也写入了 404 之外的错误信息", "message" in report)

    # 11.9 自动切换（当前模型不可用 → 切到最快可用）
    plugin._probe_concurrency = 4
    orig_probe = plugin.probe_models
    plugin.probe_models = lambda *a, **k: {
        "success": True,
        "message": "stub",
        "data": {"results": plugin._probe_results, "summary": {}},
    }
    plugin._probe_results = {
        m_a: {"status": "quota", "latency_ms": None, "at": time.time(), "code": 429, "error": "limit"},
        m_b: {"status": "ok", "latency_ms": 123, "at": time.time(), "code": 200, "error": ""},
    }
    plugin._model = m_a
    plugin._auto_switch = False
    plugin.apply_to_agent()
    report = plugin.probe_and_maybe_switch()
    check("当前模型不可用时自动切换", plugin._model == m_b, plugin._model)
    check(
        "切换原因标注为当前模型不可用",
        (report.get("switched") or {}).get("reason") == "当前模型不可用",
        str(report.get("switched")),
    )
    check("切换后同步写入 LLM_MODEL", settings.LLM_MODEL == m_b, settings.LLM_MODEL)
    check("切换后持久化到插件配置", plugin.get_config().get("model") == m_b)

    # 11.10 开着 auto_switch 且当前可用 → 仍切到更快者
    plugin._probe_results = {
        m_a: {"status": "ok", "latency_ms": 88, "at": time.time(), "code": 200, "error": ""},
        m_b: {"status": "ok", "latency_ms": 400, "at": time.time(), "code": 200, "error": ""},
    }
    plugin._model = m_b
    plugin._auto_switch = True
    plugin.probe_and_maybe_switch()
    check("开启自动切换后择优为最快模型", plugin._model == m_a, plugin._model)

    plugin._model = m_b
    plugin._auto_switch = False
    plugin.probe_and_maybe_switch()
    check("关闭自动切换且当前可用时保持不动", plugin._model == m_b, plugin._model)
    plugin.probe_models = orig_probe

    # 11.11 探测结果持久化
    cache = plugin.get_data("probe_cache")
    check(
        "探测结果写入 probe_cache",
        isinstance(cache, dict) and len(cache.get("results") or {}) > 0,
        str(type(cache)),
    )

    # 11.12 定时测速任务
    plugin._probe_enabled = True
    plugin._probe_hours = 6
    svc_ids = [s["id"] for s in plugin.get_service()]
    check(
        "开启自动测速后注册刷新+探测两个任务",
        "OpenRouterHelper.Refresh" in svc_ids and "OpenRouterHelper.Probe" in svc_ids,
        str(svc_ids),
    )
    plugin._probe_hours = 0
    check(
        "测速间隔为 0 时不注册探测任务",
        all(s["id"] != "OpenRouterHelper.Probe" for s in plugin.get_service()),
    )
    plugin._probe_hours = 6

    # 11.13 探测类 API
    openrouterhelper.requests.post = fake_post_ok
    r = plugin.api_probe_model(m_a)
    check("api_probe_model 成功", r["success"] is True and r["data"]["status"] == "ok", r["message"])
    saved_choice = plugin._model
    plugin._model = ""
    r = plugin.api_probe_model("")
    check("api_probe_model 缺参时报错", r["success"] is False and "model" in r["message"], r["message"])
    plugin._model = saved_choice
    r = plugin.api_ranking()
    check("api_ranking 返回榜单", r["success"] and r["data"]["ranked"][0]["status"] == "ok")
    r = plugin.api_models()
    check(
        "api_models 带上探测统计",
        "available" in r["data"] and r["data"]["probed"] >= 1,
        str(r["data"].get("available")),
    )
    r = plugin.api_probe()
    check("api_probe 返回汇总", r["success"] is True and r["data"]["summary"]["total"] >= 1)

    # 11.14 配置页与详情页反映测速
    plugin._probe_results = {
        m_a: {"status": "ok", "latency_ms": 250, "at": time.time(), "code": 200, "error": ""},
        m_b: {"status": "missing", "latency_ms": 90, "at": time.time(), "code": 404, "error": "no such model"},
    }
    form, defaults = plugin.get_form()
    check(
        "配置页默认值含测速开关与参数",
        all(k in defaults for k in
            ("probe_enabled", "probe_hours", "probe_concurrency", "probe_timeout",
             "probe_limit", "probe_prompt", "auto_switch")),
        str(sorted(defaults.keys())),
    )
    found = []
    walk_for_model_select(form, found)
    titles = [it["title"] for it in (found[0] if found else [])]
    check(
        "下拉标题给可用模型加 ✓ 与延迟",
        any(t.startswith("✓") and "250ms]" in t for t in titles),
        next((t for t in titles if "✓" in t), titles[0] if titles else ""),
    )
    check(
        "下拉标题给不可用模型加 ✗ 与原因",
        any(t.startswith("✗") and "模型不存在" in t for t in titles),
        next((t for t in titles if "✗" in t), ""),
    )
    page = plugin.get_page()
    rank_text = next(
        (p["props"]["text"] for p in page if "测速排行" in p["props"].get("title", "")), ""
    )
    check("详情页列出可用模型延迟", "250ms" in rank_text, rank_text[:120])
    bad_text = next(
        (p["props"]["text"] for p in page if "不可用" in p["props"].get("title", "")), ""
    )
    check("详情页列出不可用模型", m_b in bad_text, bad_text[:120])

    # 11.15 刷新目录后清理失效探测记录（模拟模型下架）
    plugin._probe_results = {
        "ghost/removed-model:free": {"status": "ok", "latency_ms": 1, "at": time.time()},
        m_a: {"status": "ok", "latency_ms": 250, "at": time.time()},
    }
    openrouterhelper.requests.get = fake_get
    plugin.refresh_models(force=True)
    check(
        "刷新后剔除已下架模型的测速记录",
        "ghost/removed-model:free" not in plugin._probe_results,
        str(list(plugin._probe_results)),
    )
    check("保留仍在列表中的测速记录", m_a in plugin._probe_results)

    print(f"\n{'=' * 46}\n结果：PASS={PASS}  FAIL={FAIL}\n{'=' * 46}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
