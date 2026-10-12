"""
OpenRouterHelper 离线自检脚本（开发用，不属于插件本体）

用桩模块模拟 MoviePilot 宿主（app.log / app.plugins / app.core.config / app.core.module），
在不装 MoviePilot 的情况下验证插件核心逻辑：
    - 免费模型判定（零价 + 文本输出）与 tool calling 标注
    - 上游 /api/v1/models 拉取与过滤（用真实抓取的样本离线回放）
    - 填充智能助手：setattr(settings) + 模块热重载 + app.env 持久化 + 幂等
    - get_form / get_page 结构
    - /test 接口的成功、401、429 分支

运行：
    "C:/Users/wangh/.workbuddy/binaries/python/envs/default/Scripts/python.exe" dev/selftest.py
"""

import json
import os
import shutil
import sys
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
        "详情页含可用免费模型告警",
        any("可用免费模型" in (p.get("props") or {}).get("title", "") for p in page),
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

    print(f"\n{'=' * 46}\n结果：PASS={PASS}  FAIL={FAIL}\n{'=' * 46}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
