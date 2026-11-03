# 设计提案：唱歌后端（sing backend）

> 状态：提案（未实现）。触发场景：用户要求 bot"唱歌"时，当前只有 TTS 念白（无旋律）。
> 本文档给出接入现有 mohobot 架构的最小方案与选型对比。

## 1. 现状与缺口

- **点歌**（`plugins/neteasemusic/`）：播放**原曲**（网易云 URL 经 NapCat record 段），不涉及合成。
- **歌曲知识**（`mohobot/music_knowledge/`）：为 LLM 注入词曲/歌词参考，LLM 可输出歌词文本。
- **TTS**（`mohobot/services/tts.py`）：念白合成（`<tts>` 标注 → 语音），**无音高/旋律**。
- 缺口：没有"按旋律生成歌声"的通路。

## 2. 选型对比

| 方案 | 形态 | 音色 | 优点 | 缺点 |
|---|---|---|---|---|
| GPT-SoVITS（so-vits 系） | 自部署 HTTP 服务 | 参考音频克隆（可用现有 TTS 音色） | 免费、可私有、音色与 TTS 一致 | 需要 GPU 推理；需伴奏/ MIDI；部署重 |
| RVC + 伴奏分离 | 自部署 | 声线转换 | 效果好 | 链路长（原曲人声分离 → 转换 → 混回） |
| ACE Studio / X Studio | 商用桌面 | 官方声库 | 质量高 | 无开放 API，无法自动化 |
| Suno/Udio 类 | 商用 API | 生成式 | 一句话出歌 | 版权/风格不可控、贵 |

**推荐**：GPT-SoVITS HTTP 服务（社区有 OpenAI 兼容封装），音色与 TTS 共用参考音频，链路最短。

## 3. 接入设计（对齐现有 TTS 模式）

```
LLM 输出 <sing>歌词</sing> 标记（复用 reply_filter 的标记识别）
    → MessageHandler 抓取标注（同 _submit_tts 模式）
    → SingService.submit(SingJob(bot_id, lyrics, song_ref))
    → 歌曲知识库匹配伴奏/MIDI（music_knowledge 已有歌名→bvid/元数据）
    → GPT-SoVITS 合成 → wav → ffmpeg 转 mp3
    → 按现有 TTS 队列发送（单飞行、丢最新、用量记账 module="sing"）
```

要点：
1. **标记识别**：`reply_filter.py` 增加 `<sing>` 别名族（对齐 `<tts>` 的模糊识别），或直接复用
   `<tts>` 标记 + `source="sing"` 由 LLM 提示词区分（改动最小，但语义混浊——建议独立标记）。
2. **伴奏来源**：全曲转写不可行；MVP 用"歌词念白 + 原曲伴奏垫底"（ffmpeg amix），
   伴奏从点歌插件已拿到的网易云 URL 分离人声（demucs, 重）或直接用纯音乐版（网易云 API 有 off vocal 版本可搜）。
3. **配置**：`sing:` 段（enabled/backend/base_url/api_key/reference_audio/queue_maxsize），
   对齐 `tts:` 结构；WebUI 复用 TTS 板块模式。
4. **计费/限流**：合成耗时远超 TTS，必须复用单飞行队列 + 每用户冷却（对齐 `/tts` 的 cmd_cooldown）。
5. **版权**：合成音频仅限私有部署自用；歌词复用受歌曲知识库同样的 CC BY-NC-SA 来源约束。

## 4. 工作量估算

| 阶段 | 内容 | 量 |
|---|---|---|
| M1 | SingService 骨架 + 标记识别 + 队列（对齐 TTS） | 1 天 |
| M2 | GPT-SoVITS 对接 + ffmpeg 混伴奏 | 1~2 天 |
| M3 | 配置/WebUI/用量/回归测试 | 1 天 |

前置条件：一台带 GPU 的机器跑 GPT-SoVITS；参考音频（洛天依音色或运维自选）。
