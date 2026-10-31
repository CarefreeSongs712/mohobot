# 日志格式 Bug：所有记录缺少换行，挤成一行

> 状态：**已修复**（PR #93，2026-10-30；一行级改动，需重启 mohobot 生效）
> 基线代码：`dac2e782`
> 发现日期：2026-10-30
> 影响面：仅日志可读性/可分析性，不影响业务功能、轮转和磁盘占用

---

## 1. 现象

`/opt/mohobot/logs/mohobot_YYYY-MM-DD.log` 里所有日志记录**首尾相连**，没有换行：

```
...text='早...'2026-10-30 15:42:11.387 | DEBUG    | mohobot.message_handler:_handle_message:653 | Skipping...
```

实测（2026-10-30 的文件，9.1MB）：

| 指标 | 数值 |
|---|---|
| 物理行数 `wc -l` | **1,216** |
| 实际日志记录（时间戳计数） | **59,970** |
| 平均每条物理行包含记录 | 约 49 条 |

journal 里同样如此（stderr 走的是另一个 handler，也缺换行）。

---

## 2. 根因

`mohobot/utils/logger.py` 中两个 format 函数的模板**末尾都没有 `\n`**，而 loguru **不会自动补换行**（默认 format 本身就以 `\n` 结尾）。

```python
def _console_format(record: dict) -> str:
    return (
        f"<green>{_utc8_time(record)}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"          # ← 缺 \n
    )


def _file_format(record: dict) -> str:
    return (
        f"{_utc8_time(record)} | "
        "{level: <8} | {name}:{function}:{line} | {message}"   # ← 缺 \n
    )
```

`logger.add(..., format=_console_format)` / `format=_file_format` 写入的就是这个字符串，所以记录之间没有任何分隔。

---

## 3. 影响

- `wc -l`、`tail -n`、`head -n`、`grep -c` 等按行工具全部失真（例如 `grep -c ERROR` 会得到 0 或错误计数）。
- 多行消息会从中间劈开记录，排查时无法还原上下文。
- 日志采集/分析工具（awk、log parser）无法按记录处理。
- **不影响**：业务功能、loguru 的按大小轮转（10MB）、保留策略（7 天）、gz 压缩、磁盘占用。

---

## 4. 修复

在 `mohobot/utils/logger.py` 两个 format 字符串末尾各加一个 `\n`：

```python
def _console_format(record: dict) -> str:
    return (
        f"<green>{_utc8_time(record)}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>\n"
    )


def _file_format(record: dict) -> str:
    return (
        f"{_utc8_time(record)} | "
        "{level: <8} | {name}:{function}:{line} | {message}\n"
    )
```

无需改 `setup_logger()` 的其他参数。`\n` 是 format 字符串里的普通转义，loguru 会原样输出；loguru 的 `<level>` 等标签不受影响。

---

## 5. 验证步骤

```bash
# 1) 部署后重启
systemctl restart mohobot

# 2) 等 30 秒，取当天文件
f=/opt/mohobot/logs/mohobot_$(date -u +%F).log

# 3) 物理行数应接近记录数（多行消息/回栈会略多）
wc -l < "$f"
grep -acE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3} \|' "$f"

# 4) 直接看尾部，应为独立成行的记录
tail -n 5 "$f"

# 5) journal 也应有换行
journalctl -u mohobot -n 20 --no-pager
```

验收：物理行数与记录数之比接近 1（修复前约 1:49）；`tail -n` 每次返回的是一条完整记录；`grep -c 'ERROR'` 计数可信。

---

## 6. 注意事项

1. **历史日志不需要改写**。修复只对之后写入的记录生效；旧的 `.log`/`.log.gz` 保持原样即可，7 天后自然淘汰。
2. **行数会突然"变多"**（约 50 倍），字节体积基本不变。如果有按行数做的监控/告警，需要同步调整阈值。
3. **回栈格式**：`backtrace=True` / `diagnose=True` 的异常栈是附在 message 之后的，修复后能正常分行显示；此前会和下一条记录粘连。
4. **不要给 message 自己补 `\n`**：只改 format，避免出现空行或双换行。
5. **部署时机**：需要重启 mohobot（约 2～3 分钟，见歌曲索引启动问题），建议和下一次常规发版一起做，不要单独重启。
6. 建议补一个轻量单测（可选）：断言 `_file_format(fake_record).endswith("\n")`，防止以后被改回去。

---

## 7. 本次不包含（独立事项）

以下问题已确认存在，但与这个格式 bug 无关，不要混在同一个改动里：

- Docker `json-file` 日志无上限（当前 375MB，两个容器 84MB/53MB）：建议在 `/etc/docker/daemon.json` 配 `max-size` / `max-file`，需重启 docker。
- `/root/QQBot/new-api/logs` 已累积 125MB，按启动生成新文件、不自动清理。
- `/opt/mohobot/review/panel.log`（当前 1.3MB）没有 logrotate。
- 文件时间戳是 UTC+8、journal 是 UTC，排查时注意换算（不是 bug）。

---

## 8. 回滚

去掉两个 `\n` 即可恢复原状；无数据迁移、无配置变更、无兼容性影响。
