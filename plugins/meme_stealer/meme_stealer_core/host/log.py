"""插件日志：转发到 mohobot 使用的 loguru。

核心代码沿用标准 logging 的调用习惯（``exc_info=True``、``%s`` 参数）。
loguru 会把多余的关键字参数当作 ``str.format`` 参数，消息里一旦含有
花括号（dict/JSON 片段）就会格式化失败，所以这里先把参数消化掉再转发。
"""

from __future__ import annotations

from typing import Any

from loguru import logger as _loguru


class PluginLogger:
    """loguru 的薄包装，接口与标准 logging.Logger 的常用子集一致。"""

    @staticmethod
    def _log(level: str, message: Any, args: tuple, exc_info: Any) -> None:
        text = str(message)
        if args:
            try:
                text = text % args
            except (TypeError, ValueError):
                text = " ".join([text, *(str(arg) for arg in args)])
        if isinstance(exc_info, BaseException):
            exception: Any = exc_info
        else:
            exception = True if exc_info else None
        # depth=2 跳过 _log 与 info()/warning() 这一层，日志定位到真实调用处
        _loguru.opt(depth=2, exception=exception).log(level, text)

    def debug(self, message: Any, *args: Any, exc_info: Any = None, **_: Any) -> None:
        self._log("DEBUG", message, args, exc_info)

    def info(self, message: Any, *args: Any, exc_info: Any = None, **_: Any) -> None:
        self._log("INFO", message, args, exc_info)

    def warning(self, message: Any, *args: Any, exc_info: Any = None, **_: Any) -> None:
        self._log("WARNING", message, args, exc_info)

    warn = warning

    def error(self, message: Any, *args: Any, exc_info: Any = None, **_: Any) -> None:
        self._log("ERROR", message, args, exc_info)

    def critical(self, message: Any, *args: Any, exc_info: Any = None, **_: Any) -> None:
        self._log("CRITICAL", message, args, exc_info)

    def exception(self, message: Any, *args: Any, exc_info: Any = True, **_: Any) -> None:
        self._log("ERROR", message, args, exc_info)


logger = PluginLogger()
