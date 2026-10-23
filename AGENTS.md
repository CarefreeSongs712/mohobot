# Mohobot 接手与开发守则

适用于整个仓库。由现有项目文档与代码整理；不授权未经审查直接推送发布分支、远程部署或业务数据变更。核对基点：`main@51b4389`。

## 1. 开始工作前

1. 阅读 [README.md](README.md)、[DEVELOPMENT.md](docs/DEVELOPMENT.md) 和 [HANDOVER.md](docs/HANDOVER.md)；先了解开发指南的「已知坑与不一致」。修改相关模块前查 [审查报告](docs/AUDIT-2026-10-12.md) 是否已有已知缺陷与方案。
2. 修改某个子目录前检查局部说明。表情包插件额外遵循 [CLAUDE.md](plugins/meme_stealer/CLAUDE.md)；主动聊天参阅其 [README.md](plugins/proactive_chat/README.md)。
3. 确认项目根目录、分支、提交和 `git status`。保留用户已有改动；不自动 reset、clean、删除未跟踪文件或覆盖配置。
4. 文档与实现不符时先核查实际调用链，报告差异；历史部署快照不能当作当前环境事实。
5. 接手整理默认不启动真实机器人，不访问生产，不使用真实密钥执行测试，不运行同步、迁移、清理脚本。
6. 本地测试使用项目 `.venv`（系统 Python 缺依赖）；可复现的隔离基线见 [HANDOVER.md](docs/HANDOVER.md) 第 6 节。

## 2. 数据与并发底线

- **history 是追加式原始归档**，不能改成 LLM 实时记忆；review 对 Mohobot 数据只读。
- **contexts 是可变工作记忆**，完成的对话轮次另写 SQLite。三者不得相互替代。
- 群聊 history 跨 bot 合并、按 message_id 去重，**只写合并布局** `history/group/{群号}.jsonl`；旧 per-bot 布局已停止写入（review 读侧仅兼容存量），不得重新引入双写。私聊 history 与 contexts 按 bot 隔离。
- 群聊最近消息、感知、情感、歌曲注解、人设等请求级输入不能直接写回上下文。引用内容沿现有文本渲染规则保存，不落原始 system 块或消息段 repr/base64。
- 人设解析顺序：私聊 session 专属 → bot 默认 → 系统默认；群聊用 bot 默认。生成捕获会话 ID、generation、人设与 bot 配置快照。
- 旧会话已删除或替换导致 `append_context` 返回 False 时，停止后续 DB/情感回写；不能把在途回复写入新活动会话。
- 通用异步 JSON/JSONL I/O 使用 [file_store.py](mohobot/file_store.py)。同一文件读改写用 `json_update`，不要分开 read/write。
- 文件锁只保证单进程协程互斥；`json_write/json_update` 的就地写入不提供崩溃原子性。人设、评估等已有临时文件替换协议不能降级。
- 人设/会话锁顺序遵守 persona service → context maintenance → chat → file；不持评估文件锁再获取人设锁，网络/LLM/渲染不占用长时间存储锁。

## 3. 主链与生命周期

- [main.py](main.py) 是装配入口；startup 与 run_forever 必须处于同一个 event loop。
- 消息先归档；外部服务独占群在归档后退出。消息封禁预检查必须在感知/观察/合并回复/LLM 之前。
- 保持 `/ab` 提前消费、私聊自动回复过滤、群 gate、多 bot 去重等顺序；拦截器链仍为封禁 → 插件 → 内置命令 → 关键词。
- `llm_excluded_groups` 只拦聊天链（含 ping），命令仍可用；`external_service_groups` 在入站归档后拦整个群事件下游，并在传输层发送前/实际 dispatch 时二次禁止向独占群发消息（不拦查询和私聊）。保留这些保护，不让后台任务/工具绕过，不能混淆两种过滤。
- 消息发送使用 [ws_server.py](mohobot/ws_server.py) 的 `send_group_msg/send_private_msg/send_image/send_group_forward_msg`，保留队列限速、echo 追踪和出站归档，不直接绕过传输层。
- 后台任务优先注册 [task_supervisor.py](mohobot/services/task_supervisor.py)，指定 name/owner；shutdown、插件重载均须取消任务并关闭网络会话/写入器。
- 面板内重启是进程内重新装配，不能当作 Python 代码发布后的进程重启。

## 4. 扩展契约

- 可配置插件用 `plugins/<name>/main.py` + `_conf_schema.json`，入口类字面名为 `Plugin`；配置从 `self.plugin_config` 读取。
- 注入方法用 `@classmethod`；`on_config_update` 必须同步。观察钩子只消费明确匹配的消息；`global_triggers` 控制多 bot 去重，不注册命令。
- 新配置检查 dataclass、load、save、to_dict、API 白名单、前端读写、示例配置与运行时同步；不能认为保存配置就自动影响旧实例。
- 服务端路径/端口不通过普通 Web 表单修改。情感/TTS 若启动时未装配实例，后续开启需重新装配/重启；已有实例的 enabled/参数可按热同步路径更新。情感 max_concurrent 与 TTS 队列容量在构造时固定，需重启，不能把“配置已保存”当作容量已变更。
- LLM 工具通过 [llm_tools.py](mohobot/services/llm_tools.py) 的 registry 注册；当前已支持任意注册名称，禁止重新引入 `song_` 前缀路由限制。
- 内置工具保留名优先；新工具应验证参数、处理缺失上下文、确认实际提供给模型的 schema 与执行路径一致。
- 数据库字段采用增量迁移及幂等守卫，不随意重建表。迁移/恢复/清理必须单独确认、备份，要求停机的操作先停机。
- 主回复产物过滤以 [reply_filter.py](mohobot/utils/reply_filter.py) 为准：朗读/格式标签保正文，工具/内部块按策略过滤；增加样本时同时覆盖流式与非流式。

## 5. 验证与完成标准

从仓库根执行，使用隔离的开发环境（`.venv/bin/python`）和临时数据：

```bash
.venv/bin/python tests/_run_all.py
.venv/bin/python tests/test_concurrency.py   # 全量 runner 排除，涉及并发/去重必须补跑
.venv/bin/python tests/test_ban_pipeline.py  # runner 漏执行其 14 个类内用例
.venv/bin/python tests/test_plugin_reload.py # runner 漏执行（只有 main）
.venv/bin/python tests/smoke_startup.py      # 修改装配/生命周期时补跑；mock 服务，无真实 sockets
.venv/bin/python -m compileall -q main.py mohobot plugins review scripts tests
```

工作区若含业务 `config/`、`data/`，优先用 [HANDOVER.md](docs/HANDOVER.md) 第 6 节的快照脚本，部分测试会探测或写入相对路径 `./data`。

- 根测试主入口是自研 [_run_all.py](tests/_run_all.py)，按文件计数、导入错误可能中断全轮；不是统一 pytest 项目。它只发现模块顶层 test_*，不自动运行 unittest 类或 main，零用例文件也会记 PASS；封禁/消息主链修改要单独执行 `python tests/test_ban_pipeline.py`，不能把文件 PASS 当作覆盖证明。
- 修改主面板 JS 时提取内联脚本并执行 `node --check`；[_js_check.py](tests/_js_check.py) 仅辅助，不能用其零退出码证明语法正确。语法检查也不能替代页面行为验收。
- 表情包插件有独立 pytest/ruff 测试，按其局部文档在插件目录运行；根 runner 不覆盖该套测试。
- 如实报告执行命令、退出码、失败原因及未覆盖项。缺依赖不是通过，历史 41/45 文件基线不是当前实测。
- 完成前检查 diff、文档链接与工作区状态；说明改了什么、验证了什么、剩余风险。不得为了「绿色」跳过测试或更改业务逻辑。

## 6. 安全、发布与许可

- 不提交 API Key、密码、Cookies、本地配置、运行数据或日志；不在交接材料里复制真实秘密。
- WebUI 无默认密码，首次按文档初始化；默认本机访问，不直接公网裸露。主面板的密钥掩码不代表插件配置也会掩码。
- **只以 main 发布**。提交、推送、SSH、重启生产、数据库同步及数据恢复都要有对应明确授权。
- 生产按 [README.md](README.md) 的流程只读预检后 `git pull --ff-only origin main`；遇脏工作区/分叉/服务不符立即暂停，不能自动强推/reset。
- 管理已核实的 systemd unit；不另起 screen/nohup/手工主进程，不影响其他项目。验收新 MainPID、端口与本次启动日志，review 是否随 unit 重启取决于 cgroup。
- 核心为 [MIT](LICENSE)，表情包与主动聊天插件各有 [AGPL-3.0](plugins/meme_stealer/LICENSE)、[AGPL-3.0](plugins/proactive_chat/LICENSE) 许可，其他移植模块保留上游声明；不能把全部插件统一当 MIT。
