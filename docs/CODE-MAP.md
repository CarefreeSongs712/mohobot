# 代码地图

本文件用于**定位代码、理解模块职责、选择修改位置**。它不复制函数签名、配置默认值或测试结果——这些以源码、`config/global.example.yaml` 和测试为准。行为细节与扩展约束见 [DEVELOPMENT](DEVELOPMENT.md)，不可破坏的规则见 [AGENTS](../AGENTS.md)。

## 1. 顶层布局

| 路径 | 职责 | 入口/关键文件 |
|---|---|---|
| `main.py` | 应用装配：创建、注入、启动、热同步、优雅关闭 | `MohobotApplication.startup()` / `shutdown()` / `restart()` |
| `mohobot/` | 框架源码（见下节） | — |
| `plugins/` | 内置插件（单文件与目录插件） | 清单见 [PLUGINS](PLUGINS.md) |
| `review/` | 独立审核面板进程（独立配置/数据库，只读 history） | `review/main.py`、`review/loader.py` |
| `scripts/` | 数据迁移、同步、维护脚本（多数需停机或授权） | 逐个脚本看 docstring |
| `tests/` | pytest 用例 + 自研 runner + fake OneBot + 冒烟 | 见 [DEVELOPMENT §1](DEVELOPMENT.md#1-开发环境与日常循环) |
| `config/` | `global.example.yaml` 模板；`global.yaml` 本地配置（gitignore） | — |
| `deploy/` | 新环境 systemd 单元模板 | 见 [DEPLOYMENT](DEPLOYMENT.md) |
| `docs/designs/` | 历史设计稿或提案；状态以文首标注为准 | — |

## 2. 源码目录（`mohobot/`）

```text
mohobot/
├─ ws_server.py            反向 WS 服务端、连接注册/鉴权、出站发送与归档
├─ bot_manager.py          Bot 实例生命周期、QQ 绑定、群内 bot 感知与抽签
├─ message_handler.py      消息主链（归档→gate→拦截器→LLM→发送）
├─ context_manager.py      会话索引、上下文读写、总结压缩、维护屏障
├─ llm_service.py          LLM 流式/工具/视觉调用与用量记录
├─ file_store.py           异步 JSON/JSONL I/O 与路径锁（新 I/O 必须走这里）
├─ history_dedup.py        群归档跨账号消息指纹
├─ image_cache.py          图片下载、phash 去重、描述缓存
├─ persona_service.py      人设预设、session 覆盖、引用检查、原子替换
├─ anysearch.py            实时搜索客户端（可选）
├─ interceptors/           封禁、插件、内置命令、关键词拦截器链
├─ services/               outbound（出站队列）、task_supervisor、usage、
│                          tts/minimax_tts、llm_tools（工具 registry）、
│                          persona_ab(+repository)、history_barrier、audit
├─ emotion/                情感系统（好感/亲密/关系阶段/长期记忆）
├─ ban/                    全局封禁名单存储与拦截器
├─ db/                     SQLite 模型、增量迁移、对话落库
├─ knowledge/              RAG 知识库（分块/向量/检索/rerank）
├─ music_knowledge/        歌曲事实库、匹配、多源同步、索引缓存
├─ news/                   新闻/动态库（审核门禁、工具、采集器，默认关）
├─ web_panel/              FastAPI 管理面板（app.py + static/index.html）
├─ models/                 OneBot 事件模型与 GlobalConfig/BotConfig
└─ utils/                  日志、CQ 码、回复过滤、卡片渲染等工具
```

## 3. 关键调用链（只指方向，细节见 DEVELOPMENT）

1. **入站**：`WSServer._handle_connection` → `_dispatch` → `MessageHandler.handle_event` → `_handle_message`。顺序（归档 → 封禁 → 插件观察 → gate → 命令 → LLM）是语义约束，见 [AGENTS §3](../AGENTS.md) 与 [DEVELOPMENT §3.1](DEVELOPMENT.md#31-_handle_message-执行顺序)。
2. **出站**：统一经 `WSServer.send_*`，由 `services/outbound.py` 每 bot 串行限速；不要绕过（否则丢 echo 追踪与出站归档）。
3. **上下文**：`ContextManager` 负责 generation/压缩/维护屏障；LLM 请求的临时 system 块（群最近消息/感知/情感/引用）只在请求期存在。
4. **审核**：`review/loader.py` 增量读 `data/history`，审核身份基于归档 `message_id`/内容指纹。
5. **配置热更新**：Web 面板保存后经 `MohobotApplication.sync_config` 分发到各组件；哪些可热更、哪些要重启见 [DEVELOPMENT §5](DEVELOPMENT.md) 与各组件的 `sync_config`。

## 4. 数据与持久化

见 [DEVELOPMENT §4](DEVELOPMENT.md)：`history/` 只追加归档、`contexts/` 工作记忆、SQLite 只存完成轮次；群聊归档为合并布局，写入去重依赖 `message_id` + 指纹。**不要把 history 当 LLM 输入，也不要把临时 system 块写回 contexts。**

## 5. 扩展入口

| 想做的事情 | 改哪里 |
|---|---|
| 加插件 | `plugins/<name>/` + `_conf_schema.json`；流程见 [DEVELOPMENT §6](DEVELOPMENT.md) 与 [PLUGINS](PLUGINS.md) |
| 加配置项 | `mohobot/models/config.py` 的 dataclass/load/save/to_dict + 面板白名单/前端/示例（8 处清单见 DEVELOPMENT §5.2） |
| 加 LLM 工具 | `mohobot/services/llm_tools.py` 的 registry（任意名称均可路由） |
| 加 DB 字段 | `mohobot/db/sql_database.py` 迁移守卫（增量迁移，不重建表） |
| 改消息路由/发送 | `message_handler.py` / `ws_server.py` / `services/outbound.py`；先读 AGENTS §3 |
| 改面板 | `mohobot/web_panel/app.py` + `static/index.html`（改 JS 需 `node --check`） |

## 6. 修改与验证

- 改动前：读 [AGENTS](../AGENTS.md)（数据/并发/生命周期/扩展/授权底线）与 [DEVELOPMENT §10](DEVELOPMENT.md)（已知坑与稳定约束）。
- 验证入口：常用 pytest、runner 覆盖边界、并发/封禁/插件重载/冒烟专项，以及 CI 实际门禁清单，见 [DEVELOPMENT §1](DEVELOPMENT.md#1-开发环境与日常循环) 与 [§12 提交前检查](DEVELOPMENT.md#12-提交前检查)。
- 生产相关：不要按本文件或开发环境推断线上状态；部署与发布按 [DEPLOYMENT](DEPLOYMENT.md) 取得授权后核实。

## 7. 需要保留的设计约束（摘要）

以下不是实现细节，而是修改时必须保持的边界；完整表述见 [AGENTS §2–§4](../AGENTS.md)：

- `startup()` 与 `run_forever()` 必须同一 event loop；
- history 追加不可变、contexts 可变、SQLite 另存完成轮次，三者不可互相替代；
- 群聊只写合并归档，不恢复 per-bot 双写；
- 人设/会话锁序 persona → maintenance → chat → file；
- 后台任务统一注册 `TaskSupervisor`（name/owner）；
- 面板内重启只是进程内重装配，不能替代代码发布后的进程重启。
