"""开发自检用的 _PluginBase 桩（只实现插件真正用到的部分）。"""

from pathlib import Path


class _PluginBase:
    plugin_name = ""
    plugin_desc = ""
    plugin_icon = ""
    plugin_version = ""
    plugin_author = ""
    plugin_config_prefix = ""
    plugin_order = 0
    auth_level = 1

    def __init__(self):
        self._data_store = {}
        self._config_store = {}
        self._data_dir = Path("./_plugin_data")
        self._data_dir.mkdir(exist_ok=True)

    # --- 持久化数据 ---
    def save_data(self, key, value):
        self._data_store[key] = value

    def get_data(self, key):
        return self._data_store.get(key)

    def del_data(self, key):
        self._data_store.pop(key, None)

    # --- 插件配置 ---
    def get_config(self):
        return self._config_store

    def update_config(self, config):
        self._config_store.update(config or {})

    def get_data_path(self):
        return self._data_dir

    def post_message(self, *a, **k):
        pass

    # --- 生命周期（子类实现）---
    def init_plugin(self, config=None):
        raise NotImplementedError

    def get_state(self):
        raise NotImplementedError

    @staticmethod
    def get_command():
        return []
