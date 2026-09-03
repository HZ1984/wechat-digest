#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公众号日报链路 · 定期自查自迭代
===============================
每日独立运行(不依赖 digest 是否成功), 巡检全链路健康度并分级处置:

  1. 数据源账号 : WeWe RSS 读书转发账号状态(source_health.account_enabled)
  2. 取文代理   : weread 取文代理可达性(source_health.proxy_ok) —— 502 时标题能抓、正文抓不到
  3. 抓取新鲜度 : 订阅源同步停滞(feeds_stale) / 本地同步过期(exported_at 过旧)
  4. 内容供给   : db_stats 正文占比(content_broken 判定)
  5. 云端跑批   : digest 今天是否真的发出了邮件(防"跑了却没发"的静默失败)
  6. 质量自迭代 : 扫描近期被过滤的高分候选, 产出"规则修正提案"(仅提案, 不自动改主规则)

三类动作(对应"能发现问题"与"能解决问题"的边界):
  A. 自愈(自动): digest 今天没跑 -> 触发 workflow_dispatch 重跑
  B. 升级(大声告警, 2~3 封主题各异, 同日不重复): 账号失效/代理挂/正文中断/同步过期/静默失败
                 -> 给出根因 + 具体操作, 但"需外部恢复/需你操作"的故障只告警不擅自改
  C. 质量自迭代(保守提案): 产出 quality_proposal.json, 待你确认后我才改 digest_cloud.py 的规则
                 -> 绝不静默放宽门槛, 以免违背"宁缺毋滥"

依赖: 仅 Python 标准库; 复用 digest_cloud 的 send_mail / send_anomaly_burst
环境变量:
  GITHUB_TOKEN       (Actions 自动注入, 用于重触发 digest / 读 workflow runs)
  GITHUB_REPOSITORY  (Actions 自动注入, owner/repo)
  SMTP_USER/SMTP_PASS/TO_EMAIL  (同 digest, 用于发告警)
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))  # 东八区
BASE_DIR = Path(__file__).resolve().parent

import digest_cloud  # 复用 send_mail / send_anomaly_burst / 评分函数

REPO = os.environ.get("GITHUB_REPOSITORY", "HZ1984/wechat-digest")
TOKEN = os.environ.get("GITHUB_TOKEN", "")


# ---------------------------------------------------------------- GitHub API 辅助
def _api(method: str, url: str, body=None, token: str = TOKEN, timeout: int = 30):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "User-Agent": "self-check", "Content-Type": "application/json"}
    req = urllib.request.Request(url, headers=headers, method=method)
    if body is not None:
        req.data = json.dumps(body).encode("utf-8")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def get_latest_digest_run_today(run_date: str, token: str) -> dict:
    """取 daily-digest 工作流今天最近一次运行(用于区分'没跑' vs '跑了却静默失败')。"""
    if not token:
        return None
    try:
        data = _api("GET",
                    f"https://api.github.com/repos/{REPO}/actions/workflows/daily-digest.yml/runs?per_page=15",
                    token=token)
        today_start = datetime.strptime(run_date, "%Y-%m-%d").replace(tzinfo=CST)
        best = None
        for r in data.get("workflow_runs", []):
            try:
                created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(CST)
            except Exception:
                continue
            if created.date() == today_start.date():
                if best is None or created > best["_created"]:
                    best = {**r, "_created": created}
        return best
    except Exception as e:
        print(f"[warn] 查询 digest 运行记录失败: {e}")
        return None


def dispatch_digest(token: str) -> bool:
    if not token:
        return False
    try:
        _api("POST",
             f"https://api.github.com/repos/{REPO}/actions/workflows/daily-digest.yml/dispatches",
             body={"ref": "main", "inputs": {}}, token=token)
        return True
    except Exception as e:
        print(f"[warn] 触发 digest 重跑失败: {e}")
        return False


# ---------------------------------------------------------------- 健康度分类
def classify(payload: dict, sent: dict, run_date: str, now: datetime, digest_run) -> tuple:
    """返回 (issues, auto_actions)。issues 元素: dict(code, severity, title, detail, fix, subject)。"""
    sh = payload.get("source_health", {})
    db = payload.get("db_stats", {})
    exported_at = payload.get("exported_at", "unknown")
    issues, actions = [], []

    # --- 1) 读书账号失效 ---
    acct_enabled = sh.get("account_enabled", 0)
    if sh.get("db_ok") and acct_enabled == 0:
        issues.append({
            "code": "account", "severity": "critical",
            "title": "读书转发账号失效",
            "detail": f"accounts 表中可用账号数 = {acct_enabled}/{sh.get('account_total')}（status=1 才可用）。"
                      f"WeWe RSS 将停止抓取新文章，后续日报会逐渐变空。",
            "fix": "请在本地浏览器打开 http://localhost:4000 重新扫码授权微信读书转发账号，账号变 status=1 后抓取自动恢复。"
                   f"（这是账号授权问题，代码无法自动修复，需要你操作一次。）",
            "subject": "【紧急·账号失效】微信读书转发账号不可用",
        })

    # --- 2) 取文代理不可用 ---
    if sh.get("proxy_ok") is False:
        issues.append({
            "code": "proxy", "severity": "critical",
            "title": "取文代理不可用",
            "detail": f"weread 取文代理 {sh.get('proxy_url')} 当前不可达（实测非 200/超时）。"
                      f"文章标题能抓到、正文却抓不下来 → 正文为空 → 日报无文可精选。",
            "fix": "这是外部服务故障，代码无法修复。① 通常数小时~1 天内自行恢复；"
                   f"② 若长期不愈，可在 wewe-rss-check/apps/server/.env.local 把 PLATFORM_URL 换成其它 weread 转发服务后重启 WeWe RSS"
                   f"（我会等你确认再改，不擅自动你的运行配置）。",
            "subject": "【紧急·代理故障】weread 取文代理不可用(502)",
        })

    # --- 3) 正文抓取中断 (db_stats 正文占比过低) ---
    r_total = db.get("recent_total")
    r_wc = db.get("recent_with_content")
    content_broken = (isinstance(r_total, int) and isinstance(r_wc, int)
                      and r_total >= 5 and r_wc <= max(2, int(r_total * 0.1)))
    if content_broken:
        issues.append({
            "code": "content_broken", "severity": "critical",
            "title": "正文抓取中断",
            "detail": f"DB 近 {db.get('days')} 天有 {r_total} 篇文，但仅 {r_wc} 篇带正文。",
            "fix": "与取文代理故障同源。代理恢复后新文自动带正文，供给自愈；无需你操作，等恢复即可。",
            "subject": "【紧急·正文中断】近N天绝大多数文章无正文",
        })

    # --- 4) 抓取/同步新鲜度 ---
    if sh.get("db_ok") and isinstance(sh.get("feeds_stale"), int) and sh["feeds_total"]:
        if sh["feeds_stale"] >= max(1, int(sh["feeds_total"] * 0.8)) and sh.get("latest_sync"):
            try:
                last = datetime.strptime(sh["latest_sync"], "%Y-%m-%d %H:%M").replace(tzinfo=CST)
                hrs = (now - last).total_seconds() / 3600
                if hrs > 24:
                    issues.append({
                        "code": "stale_feeds", "severity": "warning",
                        "title": "订阅源同步停滞",
                        "detail": f"{sh['feeds_stale']}/{sh['feeds_total']} 个订阅源超过 12h 未同步，"
                                  f"最近一次同步在 {sh['latest_sync']}（约 {hrs:.0f} 小时前）。",
                        "fix": "通常是取文代理故障的连带现象（代理挂→抓不到→sync_time 不前进）。代理恢复后自动好转；"
                               f"若代理正常却仍停滞，检查 WeWe RSS 服务是否在跑（localhost:4000）。",
                        "subject": "【注意·抓取停滞】多数订阅源长时间未同步",
                    })
            except Exception:
                pass

    # --- 5) 本地同步过期 ---
    if exported_at != "unknown":
        try:
            exp_dt = datetime.fromisoformat(exported_at)
            h = (now - exp_dt).total_seconds() / 3600
            if h > 12:
                issues.append({
                    "code": "sync_stale", "severity": "warning",
                    "title": "本地同步过期",
                    "detail": f"articles_recent.json 最后更新于 {exported_at}（北京时间），已约 {h:.0f} 小时未同步。",
                    "fix": "本机需至少每 12h 开机一次，让「WeChatDigest-Sync」计划任务（每 2h）能跑。"
                           f"若服务未起，localhost:4000 不可达时同步会跳过——手动启动 WeWe RSS 即可。",
                    "subject": "【注意·同步过期】本地数据已超过12h未更新",
                })
        except Exception:
            pass

    # --- 6) 云端跑批: digest 今天是否真发出 ---
    daily_sent = run_date in sent.values()
    anomaly_sent = f"__anomaly__{run_date}" in sent
    digest_ran = daily_sent or anomaly_sent
    if not digest_ran and now.hour >= 9:  # 已过 08:30 的跑批窗口
        silent = (digest_run is not None and digest_run.get("conclusion") == "success")
        if silent:
            issues.append({
                "code": "digest_silent", "severity": "critical",
                "title": "digest 静默失败",
                "detail": "云端日报今天已成功运行（GitHub Actions 显示 success），却没有发出任何邮件"
                          "（既无日报、也无异常告警）。属于需要排查的逻辑异常。",
                "fix": "手动触发一次 workflow_dispatch 看运行日志；重点查 digest_cloud.py 是否在异常分支提前 return "
                       "而未写 sent_history（会导致静默不发信）。",
                "subject": "【紧急·静默失败】digest 跑了却没发邮件",
            })
        else:
            # 没跑 / 跑失败 -> 尝试自动重触发
            if "--dry-run" in sys.argv:
                actions.append("（dry-run）跳过自动重触发 digest")
            elif dispatch_digest(TOKEN):
                actions.append(f"已自动触发 digest 补跑（run 将由 GitHub Actions 异步执行）")
            else:
                issues.append({
                    "code": "digest_not_run", "severity": "critical",
                    "title": "digest 未运行且无法自动重触发",
                    "detail": "今天 08:00-08:30 的定时跑批未产生任何记录（未运行或运行失败），且本环境无 GITHUB_TOKEN 无法自动重触发。",
                    "fix": "到 GitHub Actions 手动 Run workflow「Daily Digest」；检查仓库 Actions 配额/权限。",
                    "subject": "【紧急·未跑批】日报定时任务今天没运行",
                })

    return issues, actions


# ---------------------------------------------------------------- 质量自迭代(提案, 不自动改)
def quality_iterate(payload: dict, cfg: dict) -> dict:
    """扫描近期被 is_low_value 过滤、但篇幅充足且非营销的候选 -> 疑似误杀 -> 产出修正提案。
    仅产出 proposal 文件, 不修改 digest_cloud.py 主规则。
    """
    articles = payload.get("articles", [])
    quality_rules = {**digest_cloud.DEFAULT_QUALITY_RULES, **cfg.get("quality_rules", {})}
    source_blacklist = set(cfg.get("source_blacklist", []))
    candidates = []
    for a in articles:
        if a.get("source") in source_blacklist:
            continue
        raw = a.get("text", "")
        if not raw:
            continue
        try:
            a["clean_text"] = digest_cloud.clean_text(raw, quality_rules)
            if not digest_cloud.is_low_value(a, quality_rules):
                continue
            ev = digest_cloud.heuristic_score(a, cfg)
        except Exception:
            continue
        length = len(a.get("clean_text") or raw)
        is_marketing = ev.get("flags", {}).get("marketing")
        # 疑似误杀: 篇幅充足、非营销, 却被判低信息密度
        if length >= 2500 and not is_marketing:
            candidates.append({
                "title": a.get("title", "")[:60],
                "source": a.get("source", ""),
                "length": length,
                "score": round(ev.get("score", 0), 1),
                "reasons": ev.get("reasons", [])[:3],
            })
    candidates.sort(key=lambda x: x["score"], reverse=True)
    candidates = candidates[:8]

    # 历史反馈摘要
    fb_summary = {}
    fb_path = BASE_DIR / "data" / "quality_feedback.json"
    if fb_path.exists():
        try:
            fb = json.loads(fb_path.read_text(encoding="utf-8"))
            if isinstance(fb, list):
                fb_summary = {"total_cases": len(fb)}
            elif isinstance(fb, dict):
                fb_summary = {"keys": list(fb.keys())[:10], "total": sum(len(v) for v in fb.values() if isinstance(v, list))}
        except Exception:
            pass

    proposal = {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "note": "本文件为质量规则自迭代的【提案】，不会自动应用。确认后由我修改 digest_cloud.py 主规则并跑回归。",
        "suspected_false_negatives": candidates,
        "feedback_summary": fb_summary,
    }
    out = BASE_DIR / "data" / "quality_proposal.json"
    out.write_text(json.dumps(proposal, ensure_ascii=False, indent=1), encoding="utf-8")
    return proposal


# ---------------------------------------------------------------- 报告
def build_body(issues: list, sh: dict, db: dict, exported_at: str) -> str:
    sev_cn = {"critical": "🔴 紧急", "warning": "🟡 注意"}
    rows = []
    for i in issues:
        rows.append(
            f"<h3>{sev_cn.get(i['severity'], '')} {i['title']}</h3>"
            f"<p>{i['detail']}</p>"
            f"<p><b>处理建议：</b>{i['fix']}</p>"
        )
    health_lines = [
        f"读书账号可用数: {sh.get('account_enabled')}/{sh.get('account_total')}",
        f"取文代理可达: {'是' if sh.get('proxy_ok') else '否'} ({sh.get('proxy_url')})",
        f"订阅源同步停滞: {sh.get('feeds_stale')}/{sh.get('feeds_total')} (最近 {sh.get('latest_sync')})",
        f"本地同步时间: {exported_at}",
        f"DB 近{db.get('days')}天: {db.get('recent_total')} 篇, 仅 {db.get('recent_with_content')} 篇有正文",
    ]
    return (
        f"<p><b>公众号日报链路 · 每日自查发现 {len(issues)} 项需关注</b></p>"
        + "".join(rows)
        + "<hr><p><b>当前链路快照：</b></p><ul>"
        + "".join(f"<li>{l}</li>" for l in health_lines)
        + "</ul><p style='color:#888'>本邮件由 self_check 自动发出；自愈项已自动处理，需你操作的项见上方建议。</p>"
    )


def main():
    dry_run = "--dry-run" in sys.argv
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    now = datetime.now(CST)
    run_date = now.strftime("%Y-%m-%d")
    print(f"== 链路自查自迭代 · {run_date} (北京时间) ==")

    # 数据
    payload = json.loads((BASE_DIR / "data" / "articles_recent.json").read_text(encoding="utf-8"))
    hist_path = BASE_DIR / "data" / "sent_history.json"
    sent = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {}
    sh = payload.get("source_health", {})
    db = payload.get("db_stats", {})

    digest_run = get_latest_digest_run_today(run_date, TOKEN)
    issues, actions = classify(payload, sent, run_date, now, digest_run)

    # 若 digest 今天已发过异常告警(根因类), 避免与它的告警重复, 去掉 proxy/content 类
    if f"__anomaly__{run_date}" in sent:
        issues = [i for i in issues if i["code"] not in ("proxy", "content_broken")]

    # 质量自迭代提案(仅产出文件)
    proposal = quality_iterate(payload, cfg)
    print(f"[+] 质量自迭代提案: {len(proposal.get('suspected_false_negatives', []))} 条疑似误杀候选 -> data/quality_proposal.json")

    # 健康报告(始终写, 供后续复盘)
    report = {
        "run_date": run_date,
        "checked_at": now.isoformat(timespec="seconds"),
        "issues": [i["code"] for i in issues],
        "actions": actions,
        "source_health": sh,
        "db_stats": db,
    }
    (BASE_DIR / "data" / "health_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    # 处置
    if issues:
        subjects = [i["subject"] for i in issues][:3]
        body = build_body(issues, sh, db, payload.get("exported_at", "unknown"))
        digest_cloud.send_anomaly_burst(cfg, run_date, subjects, body, sent, hist_path)
    elif actions:
        info = "<p>公众号日报链路自查完成。发现 digest 今日未运行，已自动触发补跑（无需你操作）。</p>"
        if actions:
            info += "<ul>" + "".join(f"<li>{a}</li>" for a in actions) + "</ul>"
        digest_cloud.send_mail(f"公众号日报 · {run_date} · 自查已自动补跑", info, cfg)
        print("[+] 已发送'自动补跑'通知")
    else:
        print(f"[+] 链路健康, 无异常 (账号{sh.get('account_enabled')}/{sh.get('account_total')}, "
              f"代理{'可达' if sh.get('proxy_ok') else '不可达'}, 同步{ payload.get('exported_at')})")

    print("SELF_CHECK_DONE")


if __name__ == "__main__":
    main()
