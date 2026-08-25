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


def strip_noise(content_html: str) -> str:
    """剥离 script/style/svg/link/meta 等与评分无关的噪声(微信文章内嵌互动图表 JS 单块可达数百 KB)"""
    text = re.sub(r"<script[\s\S]*?</script>", "", content_html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<svg[\s\S]*?</svg>", "", text, flags=re.I)
    text = re.sub(r"<link[^>]*>|<meta[^>]*>", "", text, flags=re.I)
    return text.strip()


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
    """从 SQLite 数据库直接导出近 N 天文章(正文来自 articles.content 缓存列)。
    相比下载 /feeds/all.rss(默认仅 30 篇且需实时抓取正文):
    1) 候选池不再受 RSS limit=30 限制, 导出全部已缓存文章
    2) 无需下载 90MB+ 的 XML, 秒级完成
    3) 正文缓存由 WeWe RSS 抓取时自动写入(见 dist/feeds/feeds.service.js tryGetContent)
    """
    import sqlite3
    db_path = cfg["db_path"]
    if not os.path.exists(db_path):
        print(f"[error] 数据库不存在: {db_path}")
        return False
    lookback = datetime.now(CST).replace(tzinfo=None) - timedelta(days=cfg["days_to_export"])
    lookback_ts = int(lookback.timestamp())
    now_ts = int(time.time())
    print(f"[*] 从数据库导出近 {cfg['days_to_export']} 天文章: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.publish_time, a.content, f.mp_name AS source
            FROM articles a
            LEFT JOIN feeds f ON a.mp_id = f.id
            WHERE a.publish_time >= ? AND a.publish_time <= ?
              AND a.content IS NOT NULL AND length(a.content) > 0
            ORDER BY a.publish_time DESC
            """,
            (lookback_ts, now_ts),
        ).fetchall()
    except Exception as e:
        print(f"[error] 数据库查询失败(需要 content 列): {e}")
        conn.close()
        return False
    conn.close()

    articles = []
    for row in rows:
        dt = datetime.fromtimestamp(row["publish_time"], CST).replace(tzinfo=None)
        content_html = row["content"] or ""
        if not content_html:
            continue
        title = (row["title"] or "").strip()
        if not title:
            continue
        articles.append({
            "title": unescape(title),
            "link": f"https://mp.weixin.qq.com/s/{row['id']}",
            "pub_date": dt.strftime("%Y-%m-%d %H:%M"),
            "source": row["source"] or "",
            "content_html": strip_noise(content_html),
            "text": clean_text(content_html),
        })

    payload = {
        "exported_at": datetime.now(CST).isoformat(timespec="seconds"),
        "n_sources": len({a["source"] for a in articles if a["source"]}),
        "articles": articles,
    }
    out = BASE_DIR / "data" / "articles_recent.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"[+] 已导出 {len(articles)} 篇(近 {cfg['days_to_export']} 天, "
          f"{payload['n_sources']} 个来源) -> {out.name}"
          f" ({out.stat().st_size / 1024 / 1024:.1f} MB)")
    return True
    return True


def api_push(cfg: dict) -> bool:
    """用 GitHub Contents API 更新 data/articles_recent.json(替代 git 推送)。
    原因: 本机 git 协议访问 github.com:443 不稳定(时通时断), 而 api.github.com 稳定。
    Contents API 单文件 PUT 上限 100MB, articles_recent.json(约 2.3MB) 完全没问题。
    """
    import base64

    token = cfg["token"]
    repo_url = cfg.get("repo_url", "")
    if not token or not repo_url:
        print("[error] sync_config.json 缺少 repo_url / token, 无法推送")
        return False
    parts = repo_url.rstrip("/").replace(".git", "").split("/")
    owner, repo = parts[-2], parts[-1]
    path = "data/articles_recent.json"
    local_file = BASE_DIR / "data" / "articles_recent.json"
    if not local_file.exists():
        print(f"[error] {local_file} 不存在")
        return False

    content_b64 = base64.b64encode(local_file.read_bytes()).decode()
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}", "User-Agent": "Mozilla/5.0"}

    # 1) 获取当前文件 sha(文件不存在则为 None -> 创建)
    sha = None
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            meta = json.loads(r.read().decode())
        sha = meta.get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"[error] 获取文件 sha 失败 HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")
            return False

    # 2) PUT 更新 (带重试: 应对 GitHub 偶发 401/限流/abuse 检测)
    body = {
        "message": f"data: sync {datetime.now(CST).strftime('%Y-%m-%d %H:%M')}",
        "content": content_b64,
    }
    if sha:
        body["sha"] = sha
    last_err = ""
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                api_url,
                data=json.dumps(body).encode("utf-8"),
                headers={**headers, "Accept": "application/vnd.github+json",
                         "Content-Type": "application/json"},
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read().decode())
            print(f"[+] 已通过 GitHub API 更新 {path} -> commit {resp['commit']['sha'][:8]}")
            return True
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}"
            # 401/403/429 多为临时限流或 abuse 检测, 退避后重试
            if e.code in (401, 403, 429):
                wait = 8 * (attempt + 1)
                print(f"[warn] API 推送 {last_err}, 第 {attempt + 1} 次重试前等待 {wait}s")
                time.sleep(wait)
                continue
            print(f"[error] API 推送失败 {last_err}")
            return False
        except Exception as e:
            last_err = str(e)
            print(f"[warn] API 推送异常 {e}, 第 {attempt + 1} 次重试")
            time.sleep(5)
            continue
    print(f"[error] API 推送失败(已重试3次): {last_err}")
    return False


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
    # git 协议到 github.com:443 在本机不稳定, 已改用 GitHub Contents API
    if not api_push(cfg):
        print("[!] 同步失败: 文章已导出, 但推送至 GitHub 未完成, 云端数据未更新")
        sys.exit(1)
    print("SYNC_DONE")


if __name__ == "__main__":
    main()
