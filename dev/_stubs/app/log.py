"""开发自检用的 logger 桩。"""


class _Logger:
    def info(self, msg, *a, **k):
        print(f"[INFO ] {msg}")

    def warn(self, msg, *a, **k):
        print(f"[WARN ] {msg}")

    def warning(self, msg, *a, **k):
        print(f"[WARN ] {msg}")

    def error(self, msg, *a, **k):
        print(f"[ERROR] {msg}")

    def debug(self, msg, *a, **k):
        print(f"[DEBUG] {msg}")


logger = _Logger()
