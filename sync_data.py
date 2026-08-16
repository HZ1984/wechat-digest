#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地数据同步脚本(在用户电脑上运行)
====================================
1. 确认 WeWe RSS 服务在 localhost:4000 运行(没运行则自动拉起)
2. 下载 /feeds/all.rss(约 90MB), 解析并抽取最近 N 天文章
3. 导出为 data/articles_recent.json(仅几 MB, 避免 GitHub 100MB 限制)
4. git pull --rebase + commit + push 到 GitHub 私有仓库

配置: sync_config.json(已被 .gitignore 忽略, 含令牌勿提交)
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

CST = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
RSS_CONTENT_NS = {"content": "http://purl.org/rss/1.0-modules/content/"}

DEFAULT_CONFIG = {
    "repo_url": "",            # 如 https://github.com/<user>/<repo>.git (推送时自动嵌入令牌)
    "token": "",               # GitHub PAT (fine-grained, 需 Contents 读写权限)
    "branch": "main",
    "rss_url": "http://localhost:4000/feeds/all.rss",
    "db_path": "C:/Users/HZ/WorkBuddy/2026-08-13-18-09-38/wewe-rss-check/apps/server/data/wewe-rss.db",
    "server_dir": "C:/Users/HZ/WorkBuddy/2026-08-13-18-09-38/wewe-rss-check/apps/server",
    "node_exe": "C:/Users/HZ/.workbuddy/binaries/node/versions/22.22.2/node.exe",
    "health_url": "http://localhost:4000/",
    "days_to_export": 5,
}


def clean_text(content_html: str) -> str:
    import html as h
    text = re.sub(r"<script[\s\S]*?</script>", " ", content_html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = h.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def check_service(url: str, timeout: int = 3) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 500
    except Exception:
        return False


def start_service(cfg: dict) -> bool:
    """后台拉起 WeWe RSS 服务(node dist/main), 最多等 60 秒"""
    server_dir = Path(cfg["server_dir"])
    if not (server_dir / "dist" / "main.js").exists():
        print(f"[error] {server_dir}/dist/main.js 不存在, 无法自动拉起服务")
        return False
    print("[*] 正在后台拉起 WeWe RSS 服务...")
    try:
        subprocess.Popen(
            [cfg["node_exe"], "dist/main"],
            cwd=str(server_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception as e:
        print(f"[error] 拉起服务失败: {e}")
        return False
    for _ in range(30):
        time.sleep(2)
        if check_service(cfg["health_url"]):
            print("[+] 服务已就绪")
            return True
    print("[error] 服务 60 秒内未就绪(可能需要扫码重新登录微信读书)")
    return False


def build_source_map(db_path: str) -> dict:
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT a.id, f.mp_name FROM articles a JOIN feeds f ON a.mp_id=f.id"
        ).fetchall()
        conn.close()
        return {rid: name for rid, name in rows}
    except Exception as e:
        print(f"[warn] 数据库读取失败(来源名将缺失): {e}")
        return {}


def export_articles(cfg: dict) -> bool:
    rss_url = cfg["rss_url"]
    print(f"[*] 下载 {rss_url} (约 90MB, 请稍候)...")
    req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            xml_bytes = resp.read()
    except Exception as e:
        print(f"[error] RSS 下载失败: {e}")
        return False
    print(f"[*] 已下载 {len(xml_bytes) / 1024 / 1024:.1f} MB, 开始解析...")

    try:
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        print(f"[error] RSS 解析失败: {e}")
        return False

    source_map = build_source_map(cfg["db_path"])
    lookback = datetime.now(CST).replace(tzinfo=None) - timedelta(days=cfg["days_to_export"])
    articles = []
    for item in root.iter("item"):
        def _txt(tag):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""

        title, link = _txt("title"), _txt("link")
        if not title or not link:
            continue
        try:
            dt = parsedate_to_datetime(_txt("pubDate"))
            if dt.tzinfo:
                dt = dt.astimezone(CST).replace(tzinfo=None)
        except Exception:
            continue
        if dt < lookback:
            continue

        ce = item.find("content:encoded", RSS_CONTENT_NS)
        content_html = (ce.text or "") if ce is not None else ""
        if not content_html:
            content_html = _txt("description")

        source = ""
        if "/s/" in link:
            article_id = link.split("/s/")[-1].split("?")[0]
            source = source_map.get(article_id, "")

        articles.append({
            "title": unescape(title).strip(),
            "link": link,
            "pub_date": dt.strftime("%Y-%m-%d %H:%M"),
            "source": source,
            "content_html": content_html,
            "text": clean_text(content_html),
        })

    n_sources = len({a["source"] for a in articles if a["source"]}) or len(source_map) and len(
        set(source_map.values()))
    payload = {
        "exported_at": datetime.now(CST).isoformat(timespec="seconds"),
        "n_sources": len(set(source_map.values())) or n_sources,
        "articles": articles,
    }
    out = BASE_DIR / "data" / "articles_recent.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"[+] 已导出 {len(articles)} 篇(近 {cfg['days_to_export']} 天) -> {out.name}"
          f" ({out.stat().st_size / 1024 / 1024:.1f} MB)")
    return True


def git_push(cfg: dict) -> bool:
    if not cfg["repo_url"] or not cfg["token"]:
        print("[error] sync_config.json 缺少 repo_url / token, 无法推送")
        return False
    branch = cfg.get("branch", "main")
    push_url = cfg["repo_url"].replace("https://", f"https://x-access-token:{cfg['token']}@")
    cmds = [
        ["git", "add", "data/"],
        ["git", "-c", "user.name=local-sync", "-c", "user.email=sync@local",
         "commit", "-m", f"data: sync {datetime.now(CST).strftime('%Y-%m-%d %H:%M')}"],
        ["git", "pull", "--rebase", push_url, branch],
        ["git", "push", push_url, f"HEAD:{branch}"],
    ]
    for c in cmds:
        r = subprocess.run(c, cwd=str(BASE_DIR), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if c[1] == "commit" and r.returncode != 0 and "nothing to commit" in (r.stdout + r.stdout):
            print("[+] 数据无变化, 无需推送")
            return True
        if r.returncode != 0 and c[1] not in ("commit",):
            print(f"[error] git {' '.join(c[1:3])} 失败:\n{r.stdout}\n{r.stderr}")
            return False
        if r.returncode != 0 and c[1] == "commit" and "nothing to commit" not in r.stdout:
            print(f"[error] git commit 失败:\n{r.stdout}\n{r.stderr}")
            return False
    print("[+] 已推送到 GitHub")
    return True


def main():
    cfg_path = BASE_DIR / "sync_config.json"
    cfg = dict(DEFAULT_CONFIG)
    if cfg_path.exists():
        cfg.update(json.loads(cfg_path.read_text(encoding="utf-8")))

    if not check_service(cfg["health_url"]):
        if not start_service(cfg):
            print("[!] WeWe RSS 服务不可用, 本次同步跳过(下次开机后再试)")
            sys.exit(0)  # 不报错退出, 避免自动化任务误报失败

    if not export_articles(cfg):
        sys.exit(1)
    git_push(cfg)
    print("SYNC_DONE")


if __name__ == "__main__":
    main()
