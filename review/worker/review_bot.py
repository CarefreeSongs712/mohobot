"""Mohobot 审核面板全自动客户端: curl 拉会话 -> LLM 三态判定 -> 自动提交, 无需人工确认。
- LLM 5 路并行预取, 每个请求失败自动重试 3 次; 结论由主线程按会话顺序串行提交。
- 结论三态: 正常 / 异常 / 存疑, 按「待审#N」逐条判定; 单条存疑只跳过该条, 其余消息照常标掉。
  整页无逐条结论时回退旧格式(整页一个结论)。置信度低于 min_conf、异常缺合法标签、LLM 彻底失败 -> 保持未审。
- 只审用户消息; bot 回复只作上下文, 采纳时一并标为正常(整页只有 bot 待审时直接标正常)。
- 超过 force_after_hours(默认 72h)的待审消息不允许存疑: 提示词标「必须判决」, 不受 min_conf 限制;
  仍回存疑/缺结论/异常无合法标签时单独追问一次(只许正常/异常), 再不行按宽松标准兜底判正常。
- 常驻模式: 跑完一轮后睡到下一个 run_at 时刻(默认 09:00)再醒; 每轮重新登录; 连续存疑/失败超过 max_attempts 次不再重试。
- 生产环境用 systemd timer 每天 05:00 / 17:00(北京时间)各跑一次 --once。

用法:
  python review_bot.py                 # 常驻, 按 llm.json 每天 run_at 跑一轮
  python review_bot.py --once          # 只跑一轮就退出
  python review_bot.py --dry-run       # 只读试跑: 只打印+记日志, 不提交任何结果(自动单轮)
  python review_bot.py --at 03:30      # 临时改每天运行时刻
  python review_bot.py --dry-run --limit 5   # 只读试跑, 只看前 5 个会话
  REVIEW_PASS / REVIEW_CFG 环境变量可覆盖 llm.json 里的密码和配置路径
llm.json 示例:
  {"server": "http://103.236.70.18:9091", "user": "lingyu", "password": "...",
   "llm_base": "https://api.openai.com/v1", "llm_key": "sk-...", "llm_model": "gpt-4o-mini",
   "min_conf": 0.7, "run_at": "09:00", "max_attempts": 3, "log_path": "review_log.jsonl",
   "force_after_hours": 72}
"""
import argparse, getpass, json, os, re, subprocess, sys, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

sys.stdout.reconfigure(encoding="utf-8")
CFG = json.load(open(os.environ.get("REVIEW_CFG") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm.json"), encoding="utf-8"))
SERVER = CFG["server"].rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
PARALLEL, RETRIES = int(CFG.get("parallel", 5)), 3
MIN_CONF = float(CFG.get("min_conf", 0.7))
MAX_ATTEMPTS = int(CFG.get("max_attempts", 3))
FORCE_AFTER = float(CFG.get("force_after_hours", 72)) * 3600
RUN_AT = CFG.get("run_at") or "09:00"
LOG_PATH = CFG.get("log_path") or "review_log.jsonl"
if not os.path.isabs(LOG_PATH): LOG_PATH = os.path.join(HERE, LOG_PATH)
DRY = bool(CFG.get("dry_run"))
PROMPT = CFG.get("prompt") or (
    "你是聊天记录审核员, 标准适度宽松。\n"
    "- 只审核标注为「待审#N」的用户消息; 标注「bot, 不审」的是机器人回复, 仅作上下文, 不要评判。\n"
    "- 歌词、诗词、影视台词、小说/段子引用等创作或引用类内容, 即使涉及色情、暴力等也忽略, 不算异常。\n"
    "- 日常玩笑、口头禅、轻微粗口、擦边但不露骨的内容都算正常; 只有明确且严重的违规才判异常。\n"
    "- 用户只发了图片却没有描述时, 不要因此判存疑, 忽略图片、只按文字内容判断。\n"
    "- 广告、推广、引流、拉群、群邀请、二维码、外部链接、卡片/小程序分享等都不需要审核, 一律判正常,\n"
    "  不要判异常, 也不要判存疑; 卡片或链接看不到具体内容时同样直接判正常。\n"
    "  只有广告内容本身明确涉及色情、政治等其它违规时, 才按那项违规判断。\n"
    "- 拿不准时必须选「存疑」, 不要硬猜。常见存疑情形: 上下文不足只看得到半句话、内容明显被截断、\n"
    "  无法确认是引用还是本人言论、同一段话反复刷屏但意图不明、需要事实核查的断言。\n"
    "每条「待审#N」消息单独判定, 逐条输出一行结论(行首带同样的序号), 三选一, 字段用竖线分隔:\n"
    "  #3 结论: 正常 | 简述这条内容 | 置信度: 你对判定的把握\n"
    "  #5 结论: 异常 | 标签(只能取自 {tags}, 多个用 / 分隔) | 简述违规理由 | 置信度: 你对判定的把握\n"
    "  #7 结论: 存疑 | 简述为什么无法判断 | 置信度: 你对判定的把握\n"
    "每条待审消息必须恰好有一行结论, 不许合并、不许遗漏; 存疑只针对单条消息, 其余消息照常给出判定。\n"
    "全部结论输出完后, 最后一行写「总结: ...」简要概括这段对话。\n"
    "置信度写 0~1 的小数, 如实反映把握程度, 不要一律给高分; 低于 0.7 的会被自动跳过转人工。\n"
    "每条结论都要用自己的话具体说明看到了什么, 不许只写「无」「正常」「无异常」这类空话。\n"
    "标注「必须判决」的待审消息已积压过久, 不允许「存疑」, 只能在正常/异常中二选一;\n"
    "拿不准时按上面的宽松标准倾向正常, 仅明确违规才判异常。")
FORCE_PROMPT = (
    "下面这些待审消息已积压过久, 必须给出最终结论, 不允许「存疑」, 只能在正常/异常中二选一。\n"
    "拿不准时按宽松标准判正常, 仅明确违规才判异常。格式与之前相同, 逐条一行:\n"
    "  #N 结论: 正常 | 简述 | 置信度: 0~1\n"
    "  #N 结论: 异常 | 标签(只能取自 {tags}) | 理由 | 置信度: 0~1\n"
    "需要重判的序号: {idx}")
VERDICT_RE = re.compile(r"(?:#\s*(\d+)|(\d+)\s*[.、)])?\s*结论\s*[:：]\s*(正常|异常|存疑)(.*)")
CONF_RE = re.compile(r"置信度\s*[:：]?\s*(\d+(?:\.\d+)?%?)")
TOKEN = ""
TRIES = {}
STATS = Counter()


def curl(method, url, body=None, auth=None):
    """用 curl 发请求, 返回 (status, json); 网络失败/超时返回 status=0。"""
    args = [
        "curl", "-sS", "--connect-timeout", "10", "--max-time", "120",
        "-X", method, url, "-w", "\n%{http_code}",
    ]
    if auth: args += ["-H", f"Authorization: Bearer {auth}"]
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        args += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    try:
        r = subprocess.run(args, input=data, capture_output=True, timeout=125)
    except subprocess.TimeoutExpired:
        return 0, {"detail": "curl request timed out"}
    text, _, code = r.stdout.decode("utf-8", "replace").rpartition("\n")
    if r.returncode: return 0, {"detail": r.stderr.decode("utf-8", "replace").strip()}
    try: js = json.loads(text) if text else {}
    except ValueError: js = {"detail": text[:300]}
    return int(code or 0), js


def ok(res): return 0 < res[0] < 400


def api(method, path, body=None):
    res = curl(method, SERVER + path, body, TOKEN)
    if not ok(res): print("  ✗ 错误:", res[1].get("detail", res[1]))
    return res


def is_bot(e): return e.get("kind") == "assistant"


def forced_indices(s, now=None):
    """本页待审用户消息中超过 FORCE_AFTER 的序号集合(与「待审#N」编号一致)。"""
    now = time.time() if now is None else now
    out, seq = set(), 0
    for e in s["entries"]:
        if e.get("kind") == "summary" or is_bot(e) or e.get("status") != "unreviewed":
            continue
        seq += 1
        ts = float(e.get("timestamp") or 0)
        if FORCE_AFTER > 0 and ts > 0 and now - ts > FORCE_AFTER:
            out.add(seq)
    return out


def build_llm_req(s, tags, forced=frozenset()):
    lines, seq = [], 0
    for e in s["entries"]:
        if e.get("kind") == "summary":
            lines.append(f"[较早对话总结] {e.get('content') or ''}"); continue
        if is_bot(e):
            who, mark = "bot " + (e.get("bot_nickname") or s.get("bot_nickname") or ""), " (bot, 不审)"
        else:
            who = e.get("speaker") or e.get("role") or ""
            if e.get("status") == "unreviewed":
                seq += 1
                mark = f" (待审#{seq}, 必须判决)" if seq in forced else f" (待审#{seq})"
            else:
                mark = " (已判异常)" if e.get("status") == "abnormal" else ""
        vlm = f" [图片描述: {e['vlm']}]" if e.get("vlm") else ""
        lines.append(f"[{e.get('time_str') or ''}] {who}{mark}: {e.get('content') or ''}{vlm}")
    kind = "群聊" if s.get("chat_type") == "group" else "私聊"
    return {"model": CFG["llm_model"], "messages": [
        {"role": "system", "content": PROMPT.replace("{tags}", "/".join(tags))},
        {"role": "user", "content": f"会话: {s['display_name']} ({kind}, 第 {s['page']}/{s['pages']} 页)\n\n" + "\n".join(lines)},
    ]}


def llm_url():
    b = CFG["llm_base"].rstrip("/")
    return b if b.endswith("/chat/completions") else b + "/chat/completions"


def _parse_one(kind, rest, tags):
    """解析单条「结论: ...」行。返回 dict。"""
    body = CONF_RE.sub("", rest).strip(" |｜-—–")
    fields = [f.strip() for f in re.split(r"[|｜]", body) if f.strip()]
    conf = None
    cm = CONF_RE.search(rest)
    if cm:
        try:
            conf = float(cm.group(1).rstrip("%"))
            if conf > 1: conf /= 100
        except ValueError: conf = None
    if conf is None:  # 模型省略「置信度」二字时, 认最后一个字段里的裸数字
        for f in reversed(fields):
            if re.fullmatch(r"\d{1,3}(?:\.\d+)?%?", f):
                conf = min(float(f.rstrip("%")) / (100 if f.endswith("%") else 1), 1.0)
                fields = fields[:-1] if f == fields[-1] else fields
                break
    if kind == "正常":
        return {"action": "normal", "tags": [], "note": " ".join(fields), "conf": conf, "label": ""}
    if kind == "存疑":
        return {"action": "skip", "tags": [], "note": " | ".join(fields) or "未给原因", "conf": conf, "label": ""}
    label = fields[0] if fields else ""
    got = [t for t in re.split(r"[/,，、\s]+", label) if t in tags]
    return {"action": "abnormal", "tags": got, "note": " | ".join(fields[1:] if got else fields),
            "conf": conf, "label": label}


def parse_verdicts(text, tags):
    """解析逐条结论。返回 (per, all_v):
    per = {序号: 结论dict}(带 #N/N. 前缀的行); 无序号结论时 all_v = 整页结论(旧格式兼容)。"""
    per, all_v = {}, None
    for m in VERDICT_RE.finditer(text):
        idx = m.group(1) or m.group(2)
        v = _parse_one(m.group(3), m.group(4), tags)
        if idx is not None: per[int(idx)] = v
        else: all_v = v
    return per, all_v


def decide(v, forced=False):
    """按规则把 LLM 结论落成最终动作: 异常缺合法标签 / 无置信度 / 置信度不足 -> 跳过保持未审。

    forced(积压超时)只要求给出正常或带合法标签的异常, 不看置信度。
    """
    if v["action"] == "abnormal" and not v["tags"]:
        return dict(v, action="skip", note=f"异常但未给出合法标签「{v['label']}」, 待人工复查")
    if forced:
        return v
    if v["conf"] is None:
        return dict(v, action="skip", note="未给置信度, 待人工复查")
    if v["action"] != "skip" and v["conf"] < MIN_CONF:
        return dict(v, action="skip", note=f"置信度不足 ({v['conf']:.2f} < {MIN_CONF:.2f}): {v['note']}")
    return v


def conf_str(v):
    return f"conf={v['conf']:.2f}" if v["conf"] is not None else "conf=?"


def _post_llm(req):
    """单次请求(含 RETRIES 重试)。返回 (text, err)。"""
    err = ""
    for i in range(RETRIES + 1):
        if i: time.sleep(2 ** (i - 1))
        code, js = curl("POST", llm_url(), req, CFG["llm_key"])
        if not 0 < code < 400:
            err = f"HTTP {code}: {js.get('error', js)}"
            if 400 <= code < 500 and code != 429: break  # 参数/鉴权错误, 重试无意义
            continue
        text = ((js.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if VERDICT_RE.search(text):
            return text, ""
        err = "回复里没有「结论:」行"
    return "", f"{err} (已尝试 {i + 1} 次)"


def force_verdicts(req, text, per, forced, tags):
    """积压超时条目: 未给出有效判决的追问一次; 仍不行按宽松标准兜底判正常。

    原地更新 per, 返回追问回复文本(没追问为空)。
    """
    pending = sorted(i for i in forced if per.get(i, {}).get("action", "skip") == "skip")
    follow = ""
    if pending:
        req2 = dict(req, messages=req["messages"] + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": FORCE_PROMPT.replace("{tags}", "/".join(tags))
                .replace("{idx}", " ".join(f"#{i}" for i in pending))},
        ])
        follow, _ = _post_llm(req2)
        again, _ = parse_verdicts(follow, tags) if follow else ({}, None)
        for i in pending:
            v = again.get(i)
            v = decide(v, forced=True) if v else None
            if v and v["action"] != "skip":
                per[i] = dict(v, note="[追问] " + v["note"])
            else:
                prev = (per.get(i) or {}).get("note") or "未给结论"
                per[i] = {"action": "normal", "tags": [], "conf": None, "label": "", "fallback": True,
                          "note": f"[兜底] 积压超 {FORCE_AFTER / 3600:.0f}h 仍无法判决, 按宽松标准判正常: {prev}"}
    for i in forced:
        if i in per:
            per[i]["forced"] = True
    return follow


def ask_llm(s, tags):
    """调 LLM, 失败(网络/429/5xx/没给结论)自动重试 RETRIES 次。返回 (text, verdict, err)。"""
    forced = forced_indices(s)
    req = build_llm_req(s, tags, forced)
    text, err = _post_llm(req)
    if err:
        return "", None, err
    per, all_v = parse_verdicts(text, tags)
    if forced and not per and all_v:
        # 旧格式整页结论 + 有积压条目: 展开成逐条, 让积压条目可单独追问/兜底
        n = sum(1 for e in s["entries"] if e.get("status") == "unreviewed"
                and not is_bot(e) and e.get("kind") != "summary")
        per, all_v = {i: dict(all_v) for i in range(1, n + 1)}, None
    if per:
        per = {i: decide(v, forced=i in forced) for i, v in per.items()}
        if forced:
            follow = force_verdicts(req, text, per, forced, tags)
            if follow:
                text = text + "\n\n【积压条目追问】\n" + follow
        return text, {"per": per, "all": None}, ""
    return text, {"per": None, "all": decide(all_v)}, ""


def prepare(sk, tags, start_page=0):
    """工作线程: 取会话中有待审的一页 + 调 LLM。返回 dict 或 None(没有待审了)。

    start_page>0 时从指定页开始取(不重新锚定首条待审),
    用于逐条判定存在存疑条目的会话跳过已处理页继续向后翻。
    """
    path, page = "/api/session/" + quote(sk, safe="/:@!$&'()*+,;="), (start_page or None)
    while True:
        code, s = curl("GET", SERVER + path + (f"?page={page}" if page else ""), auth=TOKEN)
        if not 0 < code < 400: return {"sk": sk, "err": f"取会话失败 HTTP {code}: {s.get('detail', s)}"}
        pend = [e for e in s["entries"] if e.get("status") == "unreviewed"]
        if pend: break
        if s["unreviewed"] > 0 and s["page"] < s["pages"]: page = s["page"] + 1; continue
        return None
    user_fps = [e["fingerprint"] for e in pend if not is_bot(e)]
    bot_fps = [e["fingerprint"] for e in pend if is_bot(e)]
    r = {"sk": sk, "s": s, "user_fps": user_fps, "bot_fps": bot_fps}
    if user_fps: r["text"], r["verdict"], r["err"] = ask_llm(s, tags)
    return r


def submit(sk, action, fps, **extra):
    if DRY or not fps: return True
    res = api("POST", "/api/review", {"session_key": sk, "action": action, "fingerprints": fps, **extra})
    return ok(res) and res[1].get("changed", 1) > 0


def skip_session(sk):
    if not DRY: api("POST", "/api/review", {"session_key": sk, "action": "skip"})


def log_rec(sk, s, v, llm_text="", err=""):
    """追加一条 JSONL 审核记录。"""
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "dry_run": DRY, "session_key": sk,
           "session": s["display_name"], "chat_type": s.get("chat_type"), "page": s["page"], "pages": s["pages"],
           "action": (v or {}).get("action") or "llm_error", "tags": (v or {}).get("tags") or [],
           "note": (v or {}).get("note") or "", "conf": (v or {}).get("conf"),
           "user_fps": len(s.get("_user_fps", ())), "bot_fps": len(s.get("_bot_fps", ())),
           "llm": llm_text or "", "error": err}
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e: print("  ✗ 日志写入失败:", e)


def handle(r):
    """主线程: 打印 LLM 结论并提交(无人值守)。返回下一页页码(0 = 本会话本轮停/完)。"""
    s, sk, user_fps, bot_fps = r["s"], r["sk"], r["user_fps"], r["bot_fps"]
    s["_user_fps"], s["_bot_fps"] = user_fps, bot_fps
    head = f"{s['display_name']} · 第 {s['page']}/{s['pages']} 页 · 待审 用户 {len(user_fps)}/bot {len(bot_fps)}"
    if not user_fps:
        done = submit(sk, "normal", bot_fps)
        if done:
            STATS["bot_only"] += 1
        print(f"  ✓ {head}\n    → 只有 bot 回复待审, 直接标正常 ({len(bot_fps)} 条)" + ("" if done else " ✗ 提交失败"))
        log_rec(sk, s, {"action": "normal", "note": "仅 bot 待审, 直接标正常"})
        return s["page"] + 1 if done else 0
    print(f"  → {head}")
    if r["err"]:
        TRIES[sk] = TRIES.get(sk, 0) + 1
        STATS["llm_fail"] += 1
        print(f"    ✗ LLM 失败: {r['err']} → 保持未审 (连续 {TRIES[sk]}/{MAX_ATTEMPTS})")
        log_rec(sk, s, None, "", r["err"])
        skip_session(sk); return False
    v, text = r["verdict"], r["text"].strip()
    sm = re.search(r"^总结\s*[:：]\s*(.+)$", text, re.M)
    if sm: print("      【总结】" + sm.group(1).strip()[:200])
    if v.get("per") is not None:
        per = v["per"]
        groups = {"normal": [], "abnormal": [], "skip": []}
        for i, fp in enumerate(user_fps, 1):
            vd = per.get(i) or {"action": "skip", "tags": [], "note": "LLM 未给该条结论, 待人工复查", "conf": None}
            groups[vd["action"]].append((i, fp, vd))
        n_fps = [fp for _, fp, _ in groups["normal"]]
        resolved = False
        forced_n = sum(1 for g in groups.values() for _, _, vd in g if vd.get("forced"))
        fallback_n = sum(1 for _, _, vd in groups["normal"] if vd.get("fallback"))
        if forced_n:
            STATS["forced"] += forced_n
            STATS["fallback"] += fallback_n
            print(f"    ⏱ 积压超 {FORCE_AFTER / 3600:.0f}h 必须判决 {forced_n} 条" + (f" (其中兜底判正常 {fallback_n} 条)" if fallback_n else ""))
            for i, _, vd in groups["normal"]:
                if vd.get("fallback"):
                    print(f"    ⏱ #{i} {vd['note'][:160]}")
        if n_fps:
            okn = submit(sk, "normal", n_fps)
            resolved = resolved or okn
            STATS["normal"] += len(n_fps) if okn else 0
            lo = min((vd["conf"] for _, _, vd in groups["normal"] if vd["conf"] is not None), default=None)
            print(f"    ✓ {len(n_fps)} 条标正常" + (f" (最低 conf={lo:.2f})" if lo is not None else " conf=?") + ("" if okn else " ✗ 提交失败"))
        for i, fp, vd in groups["abnormal"]:
            oka = submit(sk, "abnormal", [fp], tags=vd["tags"], note="[LLM] " + vd["note"])
            resolved = resolved or oka
            STATS["abnormal"] += 1 if oka else 0
            print(f"    ⚑ #{i} 异常 {conf_str(vd)} [{'/'.join(vd['tags'])}] {vd['note']}" + ("" if oka else " ✗ 提交失败"))
        for i, fp, vd in groups["skip"]:
            print(f"    ? #{i} 存疑 {conf_str(vd)} → 保持未审: {vd['note']}")
        if resolved and bot_fps:
            okb = submit(sk, "normal", bot_fps)
            if okb: print(f"    ✓ 另 {len(bot_fps)} 条 bot 回复标正常")
        log_rec(sk, s, {"action": "mixed", "tags": [], "conf": None,
                        "note": f"逐条判定: 正常 {len(n_fps)} / 异常 {len(groups['abnormal'])} / 存疑 {len(groups['skip'])}"
                                + (f" / 积压必判 {forced_n} (兜底 {fallback_n})" if forced_n else "")}, text)
        if groups["skip"]:
            STATS["skip"] += len(groups["skip"])
            if not resolved:
                # 整页全部存疑, 无推进: 计入连续存疑, 本轮放弃该会话转人工
                TRIES[sk] = TRIES.get(sk, 0) + 1
                print(f"    ? 本页 {len(groups['skip'])}/{len(user_fps)} 条全部存疑, 无推进 (会话连续 {TRIES[sk]}/{MAX_ATTEMPTS})")
                skip_session(sk); return 0
            # 有推进: 存疑条目保持未审转人工, 会话继续翻下一页
            print(f"    ? 本页 {len(groups['skip'])}/{len(user_fps)} 条存疑转人工, 其余已提交, 继续下一页")
            return s["page"] + 1
        TRIES.pop(sk, None)
        return s["page"] + 1
    v = v["all"]
    conf = f" conf={v['conf']:.2f}" if v["conf"] is not None else " conf=?"
    if v["action"] == "normal":
        done = submit(sk, "normal", user_fps)
        if done:
            STATS["normal"] += 1
        print(f"    ✓ 正常 {conf} → {len(user_fps)} 条标正常" + ("" if done else " ✗ 提交失败"))
    elif v["action"] == "abnormal":
        done = submit(sk, "abnormal", user_fps, tags=v["tags"], note="[LLM] " + v["note"])
        if done:
            STATS["abnormal"] += 1
        print(f"    ⚑ 异常 {conf} [{'/'.join(v['tags'])}] {v['note']}")
        print("      【LLM】" + text.replace("\n", "\n      "))
        if not done: print("    ✗ 提交失败")
    else:
        TRIES[sk] = TRIES.get(sk, 0) + 1; STATS["skip"] += 1; done = False
        print(f"    ? 存疑 {conf} → 保持未审 (连续 {TRIES[sk]}/{MAX_ATTEMPTS}): {v['note']}")
        print("      【LLM】" + text.replace("\n", "\n      "))
        skip_session(sk)
    if v["action"] != "skip":
        TRIES.pop(sk, None)
        if done and bot_fps:
            done = submit(sk, "normal", bot_fps)
            print(f"    ✓ 另 {len(bot_fps)} 条 bot 回复标正常" + ("" if done else " ✗ 提交失败"))
    log_rec(sk, s, v, text)
    return s["page"] + 1 if done else 0


def login():
    global TOKEN
    pw = os.environ.get("REVIEW_PASS") or CFG.get("password") or getpass.getpass(f"{CFG['user']} 的密码: ")
    res = curl("POST", SERVER + "/api/login", {"username": CFG["user"], "password": pw})
    if not ok(res) or "token" not in res[1]:
        print("登录失败:", res[1].get("detail", res[1])); TOKEN = ""; return False
    TOKEN = res[1]["token"]; return True


def summary():
    c = STATS
    print(f"----- 本轮结束: 正常 {c['normal']} / 异常 {c['abnormal']} / 存疑跳过 {c['skip']} / "
          f"LLM失败 {c['llm_fail']} / 取会话失败 {c['fetch_fail']} / 仅bot {c['bot_only']}"
          + (f" / 积压必判 {c['forced']} (兜底正常 {c['fallback']})" if c['forced'] else "")
          + ("　(只读试跑, 未提交任何结果)" if DRY else ""))


def run_round(limit=0):
    """跑一轮全部待审会话。返回 True = 本轮有实际推进(有结论提交)。"""
    STATS.clear()
    print(f"\n===== 审核轮次 {datetime.now():%Y-%m-%d %H:%M:%S}{'  [只读试跑]' if DRY else ''} =====")
    if not login(): return False
    res = api("GET", "/api/bootstrap")
    tags = (ok(res) and res[1].get("tags")) or ["色情", "政治", "辱骂", "其他"]
    res = api("GET", "/api/sessions?" + urlencode({"bot": "", "chat_type": "", "status": "unreviewed", "sort": "oldest"}))
    allk = [x["session_key"] for x in (res[1].get("sessions", []) if ok(res) else []) if x.get("unreviewed", 0) > 0]
    keys = [k for k in allk if TRIES.get(k, 0) < MAX_ATTEMPTS]
    capped = len(allk) - len(keys)
    if capped: print(f"({capped} 个会话连续存疑/失败已达 {MAX_ATTEMPTS} 次, 本轮不再重试)")
    if not keys:
        print("没有可处理的待审会话" + (f"(另有 {capped} 个已达重试上限)" if capped else ""))
        summary()
        return False
    if limit and len(keys) > limit:
        print(f"(--limit {limit}: 只看前 {limit} 个, 实际待审 {len(keys)} 个)")
        keys = keys[:limit]
    print(f"待审会话 {len(keys)} 个, LLM {PARALLEL} 路并行预取中…")
    with ThreadPoolExecutor(PARALLEL) as pool:
        todo = {pool.submit(prepare, k, tags) for k in keys}
        try:
            while todo:
                done, todo = wait(todo, return_when=FIRST_COMPLETED)
                for f in done:
                    r = f.result()
                    if r is None: continue
                    if "s" not in r:
                        STATS["fetch_fail"] += 1
                        print(f"\n  ✗ {r['sk']}: {r['err']}"); continue
                    try: nxt = handle(r)
                    except Exception as e: nxt = 0; print(f"\n  ✗ {r['sk']} 处理出错: {e!r}")
                    if nxt and not DRY: todo.add(pool.submit(prepare, r["sk"], tags, nxt))
        except BaseException:
            for f in todo: f.cancel()
            raise
    summary()
    return bool(STATS["normal"] or STATS["abnormal"] or STATS["bot_only"])


def next_run():
    hh, _, mm = RUN_AT.partition(":")
    t = datetime.now().replace(hour=int(hh), minute=int(mm or 0), second=0, microsecond=0)
    return t if t > datetime.now() else t + timedelta(days=1)


def main():
    global RUN_AT, DRY
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出")
    ap.add_argument("--dry-run", action="store_true", help="只读试跑: 只打印+记日志, 不提交任何结果(含 skip), 自动单轮")
    ap.add_argument("--at", metavar="HH:MM", help=f"覆盖每天运行时刻(默认 {RUN_AT})")
    ap.add_argument("--limit", type=int, default=0, metavar="N", help="本轮最多处理 N 个会话(0=不限)")
    args = ap.parse_args()
    DRY, RUN_AT = DRY or args.dry_run, args.at or RUN_AT
    once = args.once or DRY
    while True:
        try: progress = run_round(args.limit)
        except Exception as e: progress = False; print("本轮异常:", e)
        if once:
            # 连环跑: 本轮有推进(且不是只读试跑)就立刻再跑一轮, 直到清空待审
            # 或本轮毫无推进(剩余全部是存疑转人工/已达重试上限)才退出
            if progress and not DRY: continue
            return
        nxt = next_run()
        print(f"下次检查: {nxt:%Y-%m-%d %H:%M} (约 {(nxt - datetime.now()).total_seconds() / 60:.0f} 分钟后)")
        while (left := (nxt - datetime.now()).total_seconds()) > 0: time.sleep(min(left, 60))


if __name__ == "__main__":
    try: main()
    except (KeyboardInterrupt, EOFError): print("\n已退出"); os._exit(1)