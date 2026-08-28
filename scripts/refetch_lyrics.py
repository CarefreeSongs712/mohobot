"""重抓所有歌词为空或仍无换行的歌曲, 用修复后的解析器就地更新。

用法:
  python scripts/refetch_lyrics.py
"""

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 待重抓条件: 歌词为空 OR 无换行(旧解析器产物)
NAMES_SQL = (
    "SELECT name FROM songs WHERE lyrics IS NULL OR lyrics = '' "
    "OR instr(lyrics, char(10)) = 0"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-folder", default="./data/song_knowledge")
    parser.add_argument("--db-file", default="knowledge_db.db")
    parser.add_argument("--cookie-file", default=None)
    parser.add_argument("--base-url", default="https://vcpedia.cn")
    args = parser.parse_args()
    db_path = str(Path(args.db_folder) / args.db_file)
    con = sqlite3.connect(db_path)
    names = [r[0] for r in con.execute(NAMES_SQL).fetchall()]
    print(f"待重抓(空或无换行): {len(names)}", flush=True)

    cookie_path = Path(args.cookie_file or (Path(args.db_folder) / "anubis_cookies.json"))
    cj = json.load(open(cookie_path, encoding="utf-8"))
    UA = "Mozilla/5.0"

    added = updated = failed = still_no_nl = 0
    fail_list: list[str] = []

    for name in names:
        # 1) 重抓 wikitext(new 解析器)
        url = (args.base_url.rstrip("/") + "/api.php?action=query&prop=revisions&rvprop=content"
               "&rvslots=main&format=json&titles=" + urllib.parse.quote(name))
        try:
            r = requests.get(url, headers={"User-Agent": UA,
                                           "Cookie": f"{cj['name']}={cj['value']}"},
                             timeout=30)
            if r.status_code != 200:
                failed += 1
                fail_list.append(name)
                time.sleep(0.8)
                continue
            page = list(r.json()["query"]["pages"].values())[0]
            if "missing" in page:
                failed += 1
                fail_list.append(name)
                time.sleep(0.8)
                continue
            src = page["revisions"][0]["slots"]["main"]["*"]
        except Exception as e:
            failed += 1
            fail_list.append(f"{name}: {e}")
            time.sleep(0.8)
            continue

        # 2) 新解析器提取
        lyrics = _parse_lyrics_from_source(src)
        if "\n" not in lyrics:
            still_no_nl += 1
        intro = _parse_introduction_from_source(src)
        credits = _parse_credits_from_lines(src.splitlines())

        # 3) 就地更新: 空解析结果保留已有内容, 防止页面格式波动破坏数据库
        old = con.execute(
            "SELECT lyrics, introduction, uploader, singers, lyricist, composer, arranger, "
            "mixer, tuner, mastering, pv, illustrator, year FROM songs WHERE name=?",
            (name,),
        ).fetchone()
        if old is None:
            continue
        values = [lyrics, intro] + [credits.get(k, "") for k in (
            "uploader", "singers", "lyricist", "composer", "arranger", "mixer",
            "tuner", "mastering", "pv", "illustrator", "year",
        )]
        values = [new if new else previous for new, previous in zip(values, old)]
        con.execute(
            "UPDATE songs SET lyrics=?, introduction=?, uploader=?, singers=?, "
            "lyricist=?, composer=?, arranger=?, mixer=?, tuner=?, mastering=?, "
            "pv=?, illustrator=?, year=? WHERE name=?",
            (*values, name),
        )
        con.commit()
        updated += 1
        time.sleep(0.8)

    con.close()
    print(f"完成: 更新 {updated}, 失败 {failed}, 重抓后仍无换行 {still_no_nl}", flush=True)
    for f in fail_list[:10]:
        print("  -", f, flush=True)


if __name__ == "__main__":
    from mohobot.music_knowledge.vcpedia import (
        _parse_credits_from_lines,
        _parse_introduction_from_source,
        _parse_lyrics_from_source,
    )
    main()