#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地触发云端日报(GitHub Actions workflow_dispatch)
====================================================
作用: 电脑开机时, 由 WorkBuddy 本地自动化在每天 07:50 调用本脚本,
     触发 GitHub 上的 Daily Digest workflow 执行选文+发信。
     与云端 schedule(08:00/08:15/08:30)互为冗余:
       - 本脚本先触发 -> 云端 schedule 触发时检测到当天已发, 自动跳过
       - 本脚本未触发(电脑关机) -> 云端 schedule 兜底(公开仓库支持)
依赖: GitHub 令牌(优先环境变量 GH_TOKEN / Windows 凭据管理器, sync_config.json 可留空)
"""

import json
import sys
import urllib.request
from pathlib import Path
from sync_data import get_github_token

BASE_DIR = Path(__file__).resolve().parent


def main():
    cfg = json.loads((BASE_DIR / "sync_config.json").read_text(encoding="utf-8"))
    token = get_github_token(cfg)
    repo_url = cfg.get("repo_url", "https://github.com/HZ1984/wechat-digest.git")
    # 从 https://github.com/OWNER/REPO.git 提取 OWNER/REPO
    parts = repo_url.rstrip("/").replace(".git", "").split("/")
    owner, repo = parts[-2], parts[-1]

    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/daily-digest.yml/dispatches"
    req = urllib.request.Request(
        url,
        data=json.dumps({"ref": "main", "inputs": {}}).encode("utf-8"),
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[+] 已触发云端日报 workflow (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        print(f"[error] 触发失败 HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")
        return 1
    except Exception as e:
        print(f"[error] 触发失败: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
