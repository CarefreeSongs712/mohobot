"""历史归档维护屏障 — 热清理/恢复时避免写入已 unlink 的旧 inode。

问题(issue #8 第 1 点): Web 面板清理/恢复 history 目录时直接 ``shutil.rmtree``,
但运行中的 ``MessageHandler._writer_registry`` 与 ``WSServer._archive_writers``
仍持有已打开的文件句柄。之后的消息继续写进**已被删除的旧 inode**, 用户看不到、
review 也读不到, 直到进程重启才恢复。

本模块提供进程内屏障:
- ``freeze()``: 置冻结标志 + 关闭两处 writer 缓存(关句柄, 不删文件);
- 冻结期间 ``frozen`` 为 True, 归档写入方应**跳过写入**(宁可这段维护窗口的
  消息不归档, 也不写进即将被删除/替换的目录);
- ``thaw()``: 清标志; 之后写入会重新 ``open`` 当前路径的文件, 指向新 inode。

不持锁跨 await: 冻结由 Web 面板在维护操作前后成对调用, 维护本身已有
``context_manager.maintenance_lock`` 等串行化保证。
"""

from __future__ import annotations

from typing import Awaitable, Callable

from loguru import logger


class HistoryBarrier:
    """历史归档维护屏障(冻结写入 + 关闭活跃 writer)。"""

    def __init__(self) -> None:
        self._frozen = False
        self._closers: list[Callable[[], Awaitable[None]]] = []

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register_closer(self, closer: Callable[[], Awaitable[None]]) -> None:
        """注册一个"关闭并清空本组件 writer 缓存"的异步回调。"""
        if closer not in self._closers:
            self._closers.append(closer)

    async def freeze(self) -> None:
        """进入维护窗口: 冻结写入并关闭所有活跃 writer。

        冻结在关闭 writer 的每个 ``await`` 点都可能被取消(维护请求断开)。
        一旦被取消/异常, ``__aenter__`` 不会执行 ``__aexit__``, 若不复位
        ``_frozen`` 就会永久跳过历史写入(issue #126 第 3 点)。因此这里把
        关闭过程包在 ``try`` 中, 任何 ``BaseException`` 都回滚冻结标志后
        继续向上抛, 让调用方知道维护窗口没有建立。
        """
        if self._frozen:
            return
        self._frozen = True
        try:
            for closer in list(self._closers):
                try:
                    await closer()
                except Exception as exc:  # noqa: BLE001 - 单个组件失败不影响其它组件
                    logger.warning(f"[HistoryBarrier] 关闭 writer 失败: {exc}")
        except BaseException:
            self._frozen = False
            logger.warning("[HistoryBarrier] 冻结被中止，已回滚冻结状态")
            raise
        logger.info(f"[HistoryBarrier] 历史归档已冻结 ({len(self._closers)} 个组件)")

    async def thaw(self) -> None:
        """退出维护窗口: 恢复写入(下次写入会重新打开当前路径)。"""
        if not self._frozen:
            return
        self._frozen = False
        logger.info("[HistoryBarrier] 历史归档已恢复")

    async def __aenter__(self) -> "HistoryBarrier":
        await self.freeze()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.thaw()


__all__ = ["HistoryBarrier"]
