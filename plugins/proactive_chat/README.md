# 私聊主动聊天 (proactive_chat)

bot 在私聊中按随机窗口主动发起消息的插件。机制与默认提示词移植自
[AstrBot 插件 astrbot_plugin_proactive_chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat)
(v1.2.6, DBJD-CR), 适配 mohobot 的消息管线与插件体系。

## 许可证

由于移植了上游插件的机制与提示词文案, 本插件遵循 **AGPL-3.0** 许可发布
(见本目录 [LICENSE](LICENSE))。若 mohobot 对外分发或仅提供网络服务,
本插件部分（含对其的修改）需按 AGPL-3.0 公开对应源码。

## 行为模型

- **自动跟踪**: 用户与 bot 的每条私聊都会登记会话并重排调度; `exclude_users` 名单内的用户不参与, 已跟踪会话随之移除。
- **随机窗口**: 用户发言 → 未回复计数归零 → 在 `[min_interval_minutes, max_interval_minutes]` 内随机取点排下一次主动消息; 发送成功后计数 +1 并排下一窗口。
- **打扰控制**:
  - 免打扰时段 `quiet_hours`(如 `23-7`, 支持跨天): 到期任务跳过并重排;
  - 连续 `max_unanswered_times` 次未回复: 暂停, 用户回复后归零恢复;
  - LLM 生成期间用户来新消息: 丢弃本次生成结果(用户已在对话, 不抢话)。
- **LLM 自主决策**(`enable_llm_choice`, 默认开): 到期触发时模型结合上下文/时间/未回复状态自主判断——
  只输出 `[SKIP]` 表示"此刻不合适"(本次不发、不计数、正常重排);
  只输出 `[PAUSE]` 表示"对方不希望被打扰"(暂停该会话, 对方回复后自动恢复)。
  判断指令为服务端固定段, 自定义提示词不影响协议; 固定次数上限仍作硬保底。
- **冷启动**: `enable_auto_trigger` 开启时, 启动后静默 `auto_trigger_after_minutes` 分钟且无消息的已跟踪会话自动补建一次任务。
- **重启恢复**: 调度状态持久化于 `data/proactive_chat/state.json`; 重启后到期任务照常触发, 停机期间已过期(>60s)的任务清理不补发, 交由自动触发或用户下一条消息重建。

## 与被动回复的关系

主动消息生成走 `llm_service.chat`(合成私聊事件, 人设/上下文/工具与被动回复一致);
发送走 `MessageHandler.send_proactive_reply`, 分段、段间延迟、TTS 标注与语音合成
完全跟随 bot 现有配置; 上下文回写只追加 assistant 一条, 主动提示词当轮生效、不落盘。

## 配置

全部配置见 Web 面板 → 插件 → proactive_chat → ⚙️ 配置(schema 驱动, 保存即热生效):

| 配置 | 默认 | 说明 |
|---|---|---|
| `enable` | true | 总开关 |
| `min_interval_minutes` / `max_interval_minutes` | 30 / 600 | 随机触发窗口(分钟) |
| `quiet_hours` | `1-7` | 免打扰时段(24h, 支持跨天) |
| `max_unanswered_times` | 4 | 连续未回复上限(0 不限制, 作硬保底) |
| `enable_llm_choice` | true | 触发时允许 LLM 自主决定发送([SKIP]/[PAUSE]) |
| `proactive_prompt` | (内置模板) | 主动开口提示词, 占位符 `{{unanswered_count}}` / `{{current_time}}` |
| `exclude_users` | [] | 排除用户 QQ 列表 |
| `enable_auto_trigger` / `auto_trigger_after_minutes` | false / 5 | 启动后静默自动补建任务 |

## 结构

```
proactive_chat/
├── main.py                  # 插件装配: 钩子转发 + 服务注入
├── _conf_schema.json        # 配置 schema(面板表单)
├── LICENSE                  # AGPL-3.0
└── proactive_chat_core/
    ├── engine.py            # 引擎: 状态持久化/调度/生成/发送/收尾
    └── schedule.py          # 纯函数: 免打扰判断/随机窗口/提示词渲染
```
