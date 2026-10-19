# REQ-3 表情包管理插件 (`meme_stealer`) 重构方案

> 对应需求: REQ-3 (重构表情包管理插件)

---

## 1. 现状痛点与重构动机

当前 `plugins/meme_stealer/` 采用“群消息全量观察（`on_message_observed`）+ 自动提取图片 + 自动调用 VLM 标注并直接入库”的设计。在生产运行中暴露了几个关键问题：

1. **偷图泛滥与资源浪费**：无论群内发送的是截图、通知图还是无关图片，都会触发下载、图片特征提取和 VLM 描述请求，消耗大量的 token 额度与磁盘存储；
2. **入库无门禁与脏数据积累**：用户发送的违规、低俗、广告或无意义表情包直接进入可用图库，Bot 在后续对话中可能会误发；
3. **缺乏 Bot 个性化与隔离**：多 Bot 共存的框架下，所有 Bot 共用同一个大图库，无法体现不同角色的性格特质（例如：傲娇角色与温柔角色发相同表情包产生违和）。

---

## 2. 重构目标与核心原则

1. **移除全量概览式偷图**：
   - 彻底关闭 `on_message_observed` 中对普通图片消息的静默抓取。
   - 仅保留两种合法入库来源：
     - **来源 A（大模型主动调用）**：通过 LLM Tool Call（`steal_meme`）显式触发偷图；
     - **来源 B（人工维护）**：在 WebUI 表情包管理面板中手动上传。
2. **引入审核流（Pending Review 机制）**：
   - 无论是 LLM 抓取还是用户上传，初始状态均为 `pending_review`（待审核）。
   - 自动生成 VLM 标签与描述，但不参与聊天语义检索和主动发送。
   - 管理员在 Web 面板中审查（查看图片、预设标签、适用的 Bot），点击“通过”后转为 `approved` 入库；点击“拒绝/删除”则丢弃。
3. **表情包适用 `bot_id` 归属与过滤**：
   - 每张表情包的元数据中增加 `allowed_bots: list[str]`。
   - 支持特定 Bot 专属（如 `["bot_001"]`）或全员通用（`["*"]` 或空列表）。
   - 在 `search_meme` 和自动选图提示（`build_meme_hint`）时，强制带入当前 Bot ID 进行作用域过滤。

---

## 3. 详细架构与数据流设计

### 3.1 状态流转图
```
[LLM Tool: steal_meme] ──┐
                         ├─▶ [生成 phash & VLM 标注] ─▶ [状态: pending_review]
[WebUI 手工单张/批量上传] ─┘                                     │
                                                                 │ (管理员操作)
                                                                 ▼
                                                 ┌───────────────┴───────────────┐
                                                 │                               │
                                                 ▼                               ▼
                                          [点击: 通过]                     [点击: 驳回/删除]
                                                 │                               │
                                                 ▼                               ▼
                                        [状态: approved]                    [从磁盘清理]
                                        [写入向量与索引库]
                                                 │
                                                 ▼
                                     [按 allowed_bots 分发检索]
```

### 3.2 数据模型变更 (`meme_stealer_core/core/models.py`)
在现有表情包元数据结构中增加字段：
```python
@dataclass
class MemeRecord:
    id: str                       # 唯一标识 (通常基于 phash 或 uuid)
    file_path: str                # 本地存储绝对/相对路径
    text_content: str             # 表情包上的文字
    description: str              # 画面描述与情感意图
    tags: list[str]               # 标签列表
    allowed_bots: list[str]       # 适用的 bot_id 列表，["*"] 表示全 Bot 通用
    status: str                   # "pending_review" | "approved" | "rejected"
    source: str                   # "llm_tool" | "webui_upload"
    created_at: float             # 时间戳
    reviewed_at: float | None     # 审核时间戳
    reviewed_by: str | None       # 审核人标识
```

### 3.3 检索层过滤适配 (`meme_stealer_core/core/engine.py`)
在向量检索和关键词匹配时加入过滤断言：
```python
def is_meme_usable_for_bot(record: MemeRecord, current_bot_id: str) -> bool:
    if record.status != "approved":
        return False
    if "*" in record.allowed_bots or not record.allowed_bots:
        return True
    return current_bot_id in record.allowed_bots
```

---

## 4. WebUI 前端与交互方案

1. **审核列表页（新增加卡片/表格）**：
   - 缩略图展示 + VLM 建议标签及文字描述；
   - 快捷修改适用 Bot（下拉多选：bot_001, bot_002 等）；
   - 一键“通过”与“驳回”；
   - 批量过审 / 批量删除操作。
2. **表情库管理页更新**：
   - 增加按 Bot 筛选查看的功能；
   - 允许对已入库的表情包重新编辑 `allowed_bots` 属性。

---

## 5. 迁移与兼容性策略

1. **历史表情包平滑兼容**：
   - 启动时自动迁移旧版本 JSON / SQLite 记录；
   - 未标注 `status` 的旧记录默认补全为 `status="approved"`；
   - 未标注 `allowed_bots` 的旧记录默认补全为 `allowed_bots=["*"]`，确保旧功能无感过渡。
2. **测试策略**：
   - 单元测试：`test_pending_review_workflow`（未审核图片不被检索）；
   - 过滤测试：`test_allowed_bots_scoping`（Bot A 不能查到属于 Bot B 的独占表情包）；
   - 工具测试：`test_steal_meme_tool_enters_review`（工具调用产物进入待审队列）。
