"""开发自检用的 ModuleManager 桩。"""


class ModuleManager:
    stopped = 0
    loaded = 0

    def stop(self):
        ModuleManager.stopped += 1

    def load_modules(self):
        ModuleManager.loaded += 1
