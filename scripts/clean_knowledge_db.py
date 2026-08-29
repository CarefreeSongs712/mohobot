# -*- coding: utf-8 -*-
"""一次性清理 knowledge_db.db 中的 wiki 冗余。

清洗规则统一在 mohobot/music_knowledge/text_clean.py 维护(爬虫入库前
也使用同一套规则), 本脚本只负责: 备份原库 → 就地清洗 → 校验。
- introduction: 去除 ''' 粗体引号、{{...}} 模板(计数模板整删、ruby/内容
  模板保留内文)、infobox/wikitable 残片、链接残留(目标|显示 → 显示)
- lyrics: 去除 HTML 标签壳(标签内正文保留)、<ref> 注释、弹幕画布残行
- credits/singers: 去除 [[ 壳与跨wiki前缀
运行前自动备份原库为 knowledge_db.db.bak-<日期>(已存在则不覆盖)。
"""
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.music_knowledge.text_clean import clean_credit, clean_introduction, clean_lyrics

DB = Path(__file__).resolve().parent.parent / "data" / "song_knowledge" / "knowledge_db.db"

CREDIT_FIELDS = ("uploader", "singers", "lyricist", "composer", "arranger",
                 "mixer", "tuner", "mastering", "pv", "illustrator")


def main():
    backup = DB.with_name(DB.name + ".bak-" + date.today().strftime("%Y%m%d"))
    if not backup.exists():
        shutil.copy2(DB, backup)
        print("已备份:", backup)
    else:
        print("备份已存在(不覆盖):", backup)

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT uuid, introduction, lyrics, " + ", ".join(CREDIT_FIELDS) + " FROM songs")
    rows = cur.fetchall()
    updates = []
    lost_content = []
    for row in rows:
        uuid, intro, lyrics = row[0], row[1], row[2]
        credits = row[3:]
        new_intro = clean_introduction(intro or "")
        new_lyrics = clean_lyrics(lyrics or "")
        new_credits = [clean_credit(x) if x else x for x in credits]
        # 校验: 清洗不得把非空字段清空
        if (intro or "").strip() and not new_intro:
            lost_content.append((uuid, "introduction"))
        if (lyrics or "").strip() and not new_lyrics:
            lost_content.append((uuid, "lyrics"))
        if new_intro != (intro or "") or new_lyrics != (lyrics or "") or new_credits != list(credits):
            updates.append([new_intro, new_lyrics, *new_credits, uuid])
    print(f"需更新 {len(updates)} / {len(rows)} 行")
    if lost_content:
        print("以下行清洗后非空字段变空(已写入, 多为 infobox/模板残渣, 请抽查):")
        for uuid, field in lost_content[:20]:
            print("  ", uuid, field)
    cur.executemany(
        "UPDATE songs SET introduction=?, lyrics=?, " + ", ".join(f + "=?" for f in CREDIT_FIELDS) +
        " WHERE uuid=?", updates)
    conn.commit()
    conn.close()
    print("完成")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
