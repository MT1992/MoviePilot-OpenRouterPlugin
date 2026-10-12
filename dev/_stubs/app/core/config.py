"""开发自检用的 settings 桩。"""

from pathlib import Path


class _Settings:
    # 智能助手相关
    AI_AGENT_ENABLE = False
    LLM_PROVIDER = "deepseek"
    LLM_MODEL = "deepseek-chat"
    LLM_API_KEY = ""
    LLM_BASE_URL = "https://api.deepseek.com"
    LLM_API_PROTOCOL = "auto"
    # 代理
    PROXY_HOST = None
    # 配置目录
    CONFIG_PATH = Path("./_config")

    # 记录 setattr 历史，便于断言
    def __init__(self):
        object.__setattr__(self, "_changes", {})

    def __setattr__(self, name, value):
        self._changes[name] = value
        object.__setattr__(self, name, value)


settings = _Settings()
