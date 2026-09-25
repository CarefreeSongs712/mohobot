"""一次性迁移+扫尾: 删除旧 bot_x/group/{群号} 键结论, 全量待审判 normal (审核人 admin)。"""
import sys, time
sys.path.insert(0, "/root/QQBot/mohobot")
from review.loader import MohobotData
from review.store import ReviewStore

store = ReviewStore("/root/QQBot/mohobot/review/data/review.db")

# 1. 迁移: 旧键 bot_x/group/{群号} 的结论(全部 normal)废弃, 新 key 下统一重判
old = store._conn.execute(
    "SELECT COUNT(*) FROM reviewed_entries WHERE session_key LIKE '%/group/%' "
    "AND session_key NOT LIKE '_merged/%'").fetchone()[0]
store._conn.execute(
    "DELETE FROM reviewed_entries WHERE session_key LIKE '%/group/%' "
    "AND session_key NOT LIKE '_merged/group/%'")
store._conn.commit()
print(f"已删除旧群 key 结论: {old} 条")

# 2. 全量扫尾: 把当前所有待审(含上次残留与新进)判为 normal
data = MohobotData("/root/QQBot/mohobot/data")
t0 = time.time()
sessions = data.list_sessions(force=True)
judged = 0
n_done = 0
n_group = 0
for s in sessions:
    sk = s["session_key"]
    statuses = store.statuses_by_session().get(sk, {})
    entries = data.enrich_entries(sk, statuses, store.abnormal_by_fingerprint())
    fps = [e["fingerprint"] for e in entries if e["status"] == "unreviewed"]
    if fps:
        store.judge(sk, fps, "normal", "admin")
        judged += len(fps)
    if s["chat_type"] == "group":
        n_group += 1
    n_done += 1
    if n_done % 200 == 0:
        print(f"  进度 {n_done}/{len(sessions)}, 已判 {judged}", flush=True)
print(f"扫尾完成: {len(sessions)} 个会话(群聊合并会话 {n_group} 个), "
      f"补判 {judged} 条, 耗时 {time.time()-t0:.0f}s")
n = store._conn.execute("select count(*) from reviewed_entries").fetchone()[0]
a = store._conn.execute("select count(*) from abnormal_records").fetchone()[0]
print("reviewed_entries:", n, "| abnormal_records:", a)
store.close()
