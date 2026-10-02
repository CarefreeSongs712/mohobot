"""插件配置的持久化：``data/plugins_config/{插件名}.json``。

mohobot 面板读写的是同一个文件；插件自己改配置（``/meme on``、WebUI 设置）
时写回这里，面板再打开就能看到最新值。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .log import logger


class ConfigStore(dict):
    """dict 形式的配置快照，``save_config`` 原子写回磁盘。"""

    def __init__(self, path: Path | str | None, initial: dict[str, Any] | None = None) -> None:
        super().__init__(initial or {})
        self.path = Path(path) if path else None

    def _read_disk(self) -> dict[str, Any]:
        if self.path is None or not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[Config] 读取插件配置失败 {self.path}: {e}")
            return {}
        return data if isinstance(data, dict) else {}

    def save_config(self, updates: dict[str, Any] | None = None) -> bool:
        """把 ``updates`` 合并进磁盘存档；不传时用整个快照覆盖存档。

        增量写以磁盘存档为底，保留面板刚写入、内存里还没有的键。
        """
        if updates:
            self.update(updates)
        if self.path is None:
            return True
        if updates:
            merged = self._read_disk()
            merged.update(updates)
        else:
            merged = dict(self)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(merged, handle, ensure_ascii=False, indent=2)
                os.replace(temp_path, self.path)
            except BaseException:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise
            return True
        except OSError as e:
            logger.error(f"[Config] 写入插件配置失败 {self.path}: {e}")
            return False
