#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在 cron-job.org 创建"微信日报云端触发"定时任务(一次性配置脚本)
==============================================================
作用: 每天 07:45 (北京时间) 由 cron-job.org 调用 GitHub workflow_dispatch API,
     触发 Daily Digest workflow 执行选文+发信。
     电脑关机时也能保证早上起床看到日报(不再依赖 GitHub Actions schedule)。

依赖:
  - cron-job.org API Key(https://console.cron-job.org -> Settings 获取)
  - GitHub token(优先环境变量 GH_TOKEN / Windows 凭据管理器, sync_config.json 可留空)

用法:
  python setup_cronjob.py <CRONJOB_API_KEY>
"""
import json
import sys
import urllib.request
from pathlib import Path
from sync_data import get_github_token

BASE_DIR = Path(__file__).resolve().parent
API = "https://api.cron-job.org/jobs"


def main():
    if len(sys.argv) < 2:
        print("用法: python setup_cronjob.py <cron-job.org API Key>")
        return 1
    api_key = sys.argv[1].strip()

    cfg = json.loads((BASE_DIR / "sync_config.json").read_text(encoding="utf-8"))
    gh_token = get_github_token(cfg)
    repo_url = cfg.get("repo_url", "https://github.com/HZ1984/wechat-digest.git")
    parts = repo_url.rstrip("/").replace(".git", "").split("/")
    owner, repo = parts[-2], parts[-1]
    dispatch_url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/daily-digest.yml/dispatches"

    payload = {
        "job": {
            "enabled": True,
            "title": "微信日报-云端触发(08:00发信兜底)",
            "saveResponses": True,
            "url": dispatch_url,
            "requestMethod": 1,          # 1 = POST
            "extendedData": {
                "headers": {
                    "Authorization": f"token {gh_token}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                },
                "body": json.dumps({"ref": "main", "inputs": {}}),
            },
            "schedule": {
                "timezone": "Asia/Shanghai",
                "hours": [7],
                "minutes": [45],
                "mdays": [-1],
                "months": [-1],
                "wdays": [-1],
            },
        }
    }

    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[+] cron-job.org 任务创建成功 (HTTP {resp.status})")
            print(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        print(f"[error] HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")
        return 1
    except Exception as e:
        print(f"[error] {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
