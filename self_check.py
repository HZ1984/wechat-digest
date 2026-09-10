#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公众号日报链路 · 定期自查自迭代（WeRSS 自托管架构版）
========================================================
2026-09-09 重构: 原 WeWe RSS(走 weread.111965.xyz 代理)已弃用, 改为本地自托管
WeRSS(rachelos/we-mp-rss, 端口 8001), 微信源池改为 data/werss_articles.json
(由本机 WeRSS-Now 计划任务每 3h 推送)。本脚本随之改为"多池数据驱动"巡检:

  数据源(三池, 均在仓库 data/ 下):
    1. werss  WeRSS 自托管(微信)  —— 本机每 3h 推送, 机器关机则停滞
    2. rss    RSS 媒体源           —— GitHub Actions 每 2h 云端抓取
    3. web    官网直抓             —— GitHub Actions 云端抓取

  巡检项(分级处置):
    A. WeRSS 微信池: 数据缺失 / 源数偏少 / 正文抓取中断 / 本地同步过期
    B. RSS / Web 池: 数据缺失 / 云端同步停滞(>24h 警告, >48h 紧急)
    C. 云端跑批    : digest 今天是否真发出(防"跑了却没发"的静默失败) -> 自动补触发
    D. 质量自迭代  : 扫描近期被过滤的高分候选, 产出"规则修正提案"(仅提案, 不自动改主规则)

  自愈(自动): digest 今天没跑 -> 触发 workflow_dispatch 重跑
  升级(大声告警, 同日不重复): 微信池缺失/正文中断/静默失败/云端池停滞
  质量自迭代(保守提案): 产出 quality_proposal.json, 待确认后才改 digest_cloud.py 主规则

依赖: 仅 Python 标准库; 复用 digest_cloud 的 send_mail / send_anomaly_burst / 评分函数。
环境变量(本地 dry-run 或 Actions 注入):
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

# 三池定义: (key, 文件路径, 中文标签, 来源)
# 来源 local  = 本机计划任务推送, 依赖用户开机; cloud = GitHub Actions 云端, 不依赖本机
POOLS = [
    ("werss", "data/werss_articles.json", "WeRSS 自托管(微信)", "local"),
    ("rss", "data/rss_articles.json", "RSS 媒体源", "cloud"),
    ("web", "data/web_articles.json", "官网直抓", "cloud"),
]

# 阈值(小时)
WERSS_STALE_H = 12          # 本机微信池超过此值视为过期(机器未开机时顺延)
WERSS_DEFER_H = 24          # 超过此值视为"本机可能未开机", 抑制微信池新鲜度误报
CLOUD_STALE_H = 24          # 云端池超过此值警告
CLOUD_CRIT_H = 48           # 云端池超过此值紧急
WERSS_MIN_SOURCES = 50      # 微信源正常约 62, 低于此值告警
WERSS_CONTENT_RATIO = 0.7   # 微信池正文占比低于此值视为正文中断


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


# ---------------------------------------------------------------- 数据解析辅助
def parse_exported(s):
    """解析各池 exported_at 字段(格式不统一: ISO+时区 / 'YYYY-MM-DD HH:MM'), 统一返回东八区 datetime。"""
    if not s or s == "unknown":
        return None
    s = str(s).strip()
    dt = None
    try:
        dt = datetime.fromisoformat(s)  # 处理 ISO(含 +08:00 / Z)
    except Exception:
        pass
    if dt is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except Exception:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt


def _has_content(a: dict) -> bool:
    t = a.get("content_html") or a.get("text") or ""
    return len((t or "").strip()) >= 200


def _latest_pub_dt(arts: list) -> datetime:
    """取池内最新发布时间(publish_time 多为 unix 秒)。返回 CST datetime 或 None。"""
    best = None
    for a in arts:
        p = a.get("publish_time") or a.get("pub_date")
        if isinstance(p, (int, float)) and p > 0:
            try:
                dt = datetime.fromtimestamp(p, tz=CST)
            except Exception:
                continue
        elif isinstance(p, str):
            dt = parse_exported(p)
        else:
            continue
        if dt and (best is None or dt > best):
            best = dt
    return best


def pool_stats(rel: str, now: datetime) -> dict:
    """读取单个池文件, 计算健康快照。文件缺失/损坏时 present=False。"""
    path = BASE_DIR / rel
    if not path.exists():
        return {"present": False, "rel": rel, "exported_at": "missing",
                "exported_dt": None, "age_h": 0.0, "count": 0,
                "with_content": 0, "content_ratio": 0.0,
                "latest_pub_dt": None, "latest_pub_str": "—", "n_sources": 0}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[warn] 读取 {rel} 失败: {e}")
        return {"present": False, "rel": rel, "exported_at": f"corrupt({e})",
                "exported_dt": None, "age_h": 0.0, "count": 0,
                "with_content": 0, "content_ratio": 0.0,
                "latest_pub_dt": None, "latest_pub_str": "—", "n_sources": 0}
    arts = d.get("articles") if isinstance(d, dict) else None
    if not isinstance(arts, list):
        arts = []
    exp = parse_exported(d.get("exported_at")) if isinstance(d, dict) else None
    age_h = (now - exp).total_seconds() / 3600 if exp else 0.0
    wc = sum(1 for a in arts if _has_content(a))
    lp = _latest_pub_dt(arts)
    return {
        "present": True,
        "rel": rel,
        "exported_at": d.get("exported_at", "unknown") if isinstance(d, dict) else "unknown",
        "exported_dt": exp,
        "age_h": round(age_h, 1),
        "count": len(arts),
        "with_content": wc,
        "content_ratio": (wc / len(arts)) if arts else 0.0,
        "latest_pub_dt": lp,
        "latest_pub_str": lp.strftime("%Y-%m-%d %H:%M") if lp else "—",
        "n_sources": d.get("n_sources", 0) if isinstance(d, dict) else 0,
    }


def compute_pools(now: datetime) -> dict:
    out = {}
    for key, rel, label, kind in POOLS:
        st = pool_stats(rel, now)
        st["label"] = label
        st["kind"] = kind
        out[key] = st
    return out


# ---------------------------------------------------------------- 健康度分类
def classify(pools: dict, sent: dict, run_date: str, now: datetime, digest_run,
             deferred: bool = False) -> tuple:
    """返回 (issues, actions)。issues 元素: dict(code, severity, title, detail, fix, subject)。"""
    issues, actions = [], []
    werss = pools.get("werss")
    rss = pools.get("rss")
    web = pools.get("web")

    # ===== A. WeRSS 微信池(本地推送, 机器关机会停滞) =====
    if not (werss and werss["present"]):
        issues.append({
            "code": "werss_missing", "severity": "critical",
            "title": "WeRSS 微信池数据缺失",
            "detail": "云端未找到 data/werss_articles.json, 或本地从未推送。微信源是日报主干, 缺失将导致日报几乎无文可精选。",
            "fix": "在本地 werss 目录运行 powershell 执行 run_werss_sync.ps1 (或等本机 WeRSS-Now 计划任务), "
                   "把 werss_articles.json 推送到 GitHub。",
            "subject": "【紧急·数据缺失】WeRSS 微信池未推送",
        })
    else:
        # 源数偏少
        ns = werss.get("n_sources") or 0
        if ns and ns < WERSS_MIN_SOURCES:
            issues.append({
                "code": "werss_sources", "severity": "warning",
                "title": "WeRSS 微信源数量偏少",
                "detail": f"WeRSS 当前 {ns} 个源(正常约 62)。可能是 manage_feeds 误删或同步不全。",
                "fix": "在本地 werss 目录运行 python manage_feeds.py list 核对; 如需恢复用 remove 的逆操作或重新添加。",
                "subject": "【注意·源数偏少】WeRSS 公众号源少于预期",
            })
        # 正文抓取中断
        if werss["count"] >= 5 and werss["content_ratio"] < WERSS_CONTENT_RATIO:
            issues.append({
                "code": "werss_content", "severity": "critical",
                "title": "WeRSS 微信正文抓取中断",
                "detail": f"WeRSS 池 {werss['count']} 篇中仅 {werss['with_content']} 篇带正文(占比 {werss['content_ratio']*100:.0f}%)。",
                "fix": "WeRSS 已设置 gather.content=True。若突然大量无正文, 多为微信读书账号会话失效; "
                       "在本地浏览器打开 http://localhost:8001 重新登录微信读书账号后, 下次同步自动恢复。",
                "subject": "【紧急·正文中断】WeRSS 微信文章大量无正文",
            })
        # 本地同步过期(机器未开机时顺延)
        if not deferred and werss["age_h"] > WERSS_STALE_H:
            issues.append({
                "code": "werss_stale", "severity": "warning",
                "title": "WeRSS 本地同步过期",
                "detail": f"werss_articles.json 最后更新于 {werss['exported_at']}(约 {werss['age_h']:.0f} 小时前)。"
                          f"本机 WeRSS-Now 计划任务应每 3 小时推送一次。",
                "fix": "确认本机已开机且 WeRSS-Now 计划任务在运行(任务计划程序里看 NextRun/LastResult)。"
                       f"若服务没起, 手动运行 run_werss_sync.ps1 拉起。",
                "subject": "【注意·同步过期】WeRSS 本地数据已超过12h未更新",
            })

    # ===== B. RSS / Web 云端池(不依赖本机, 应始终新鲜) =====
    for key in ("rss", "web"):
        p = pools.get(key)
        label = next(l for k, _, l, _ in POOLS if k == key)
        rel = next(r for k, r, _, _ in POOLS if k == key)
        if not (p and p["present"]):
            issues.append({
                "code": f"{key}_missing", "severity": "warning",
                "title": f"{label}数据缺失",
                "detail": f"云端未找到 {rel}。该池由 GitHub Actions 云端抓取, 缺失说明对应工作流未运行或推送失败。",
                "fix": f"到 GitHub Actions 查看对应工作流最近运行是否 success; 必要时手动 Run workflow。",
                "subject": f"【注意·数据缺失】{label}未生成",
            })
            continue
        if p["age_h"] > CLOUD_STALE_H:
            sev = "critical" if p["age_h"] > CLOUD_CRIT_H else "warning"
            issues.append({
                "code": f"{key}_stale", "severity": sev,
                "title": f"{label}同步停滞",
                "detail": f"{rel} 最后更新于 {p['exported_at']}(约 {p['age_h']:.0f} 小时前)。"
                          f"该池由 GitHub Actions 云端每 2 小时抓取, 长时间不更新说明云端工作流可能失败。",
                "fix": "到 GitHub Actions 查看 RSS Sync / Web 抓取工作流最近运行是否 success; 必要时手动 Run workflow。",
                "subject": f"【{'紧急' if sev=='critical' else '注意'}·同步停滞】{label}长时间未更新",
            })

    # ===== B2. 错误正文扫描(抓取层把 API 错误回显当正文) =====
    # 2026-09-10 实测: 盖世汽车社区 / Barrons巴伦 / 新京报书评周刊 三篇曾以"参数错误"开头
    # 被误发进日报。日报侧已加双闸门拦截(werss_to_digest + digest_cloud), 此处负责"看见"它。
    for key, rel, label, kind in POOLS:
        p = BASE_DIR / rel
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        arts = d.get("articles") if isinstance(d, dict) else None
        if not isinstance(arts, list):
            continue
        bad = sum(1 for a in arts
                  if digest_cloud.is_error_text(a.get("text") or a.get("content_html") or ""))
        if bad:
            sev = "critical" if bad >= 3 else "warning"
            issues.append({
                "code": f"{key}_error_text", "severity": sev,
                "title": f"{label}发现 {bad} 篇疑似抓取错误正文",
                "detail": (f"{rel} 中有 {bad} 篇文章正文以“参数错误”等错误短语开头, "
                           f"说明抓取层把微信 API 的错误回显当成了正文。日报侧已加双闸门拦截, 不会误发, "
                           f"但提示该源抓取不稳定, 需关注。"),
                "fix": (f"等待下次同步自动重抓即可修复; 若某源频繁出现, 在本地检查 WeRSS 微信读书账号会话是否失效 "
                        f"(打开 http://localhost:8001 重新登录), 或调大抓取重试。"),
                "subject": f"【{'紧急' if sev=='critical' else '注意'}·脏数据】{label}含 {bad} 篇错误正文",
            })

    # ===== C. 云端跑批: digest 今天是否真发出 =====
    daily_sent = run_date in sent.values()
    anomaly_sent = f"__anomaly__{run_date}" in sent
    digest_ran = daily_sent or anomaly_sent
    if not digest_ran and now.hour >= 9:  # 已过 08:30 的跑批窗口
        silent = (digest_run is not None and digest_run.get("conclusion") == "success")
        if silent:
            issues.append({
                "code": "digest_silent", "severity": "critical",
                "title": "digest 静默失败",
                "detail": "云端日报今天已成功运行(GitHub Actions 显示 success), 却没有发出任何邮件"
                          "(既无日报、也无异常告警)。属于需要排查的逻辑异常。",
                "fix": "手动触发一次 workflow_dispatch 看运行日志; 重点查 digest_cloud.py 是否在异常分支提前 return "
                       "而未写 sent_history(会导致静默不发信)。",
                "subject": "【紧急·静默失败】digest 跑了却没发邮件",
            })
        else:
            if "--dry-run" in sys.argv:
                actions.append("（dry-run）跳过自动重触发 digest")
            elif dispatch_digest(TOKEN):
                actions.append("已自动触发 digest 补跑(run 将由 GitHub Actions 异步执行)")
            else:
                issues.append({
                    "code": "digest_not_run", "severity": "critical",
                    "title": "digest 未运行且无法自动重触发",
                    "detail": "今天 08:00-08:30 的定时跑批未产生任何记录(未运行或运行失败), 且本环境无 GITHUB_TOKEN 无法自动重触发。",
                    "fix": "到 GitHub Actions 手动 Run workflow「Daily Digest」; 检查仓库 Actions 配额/权限。",
                    "subject": "【紧急·未跑批】日报定时任务今天没运行",
                })

    # ===== 顺延逻辑: 本机未开机(werss 距上次同步>24h)时, 抑制"微信池新鲜度"误报 =====
    if deferred:
        issues = [i for i in issues if i["code"] != "werss_stale"]

    return issues, actions


# ---------------------------------------------------------------- 质量自迭代(提案, 不自动改)
def quality_iterate(pools: dict, cfg: dict) -> dict:
    """委托给 quality_iteration 模块(三池扫描 + 回归 + 双向扫描 + 检索词生成)。
    2026-09-09 起微信池已改为 werss_articles.json(见 quality_iteration.POOLS)。
    """
    import quality_iteration as QI
    return QI.run(BASE_DIR, cfg)


def _legacy_quality_iterate(payload: dict, cfg: dict) -> dict:
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
def quality_section(proposal: dict) -> str:
    """把筛选机制自迭代的结果渲染成邮件小节, 让每日邮件能看到迭代方向。"""
    if not proposal:
        return ""
    reg = proposal.get("regression", {})
    icon = "✓" if reg.get("status") == "pass" else "✗"
    lines = [
        f"<li><b>回归测试</b>: {icon} {reg.get('passed', '?')}/{reg.get('total', '?')} 通过"
        f"（质量红线, 不通过即视为回退）</li>",
    ]
    pools = proposal.get("pool_sizes", {})
    if pools:
        lines.append("<li><b>源池</b>: " +
                     "、".join(f"{k} {v} 篇" for k, v in pools.items()) + "</li>")

    fn = proposal.get("suspected_false_negatives", [])
    fp = proposal.get("suspected_false_positives", [])
    lines.append(f"<li><b>疑似误杀</b>(好文被过滤): {len(fn)} 条</li>")
    lines.append(f"<li><b>疑似漏网</b>(低质却高分): {len(fp)} 条</li>")
    for it in fp[:4]:
        kind = "/".join(it.get("signals", {}).keys())
        lines.append(f"<li style='color:#b26a00'>　! {it.get('score')}分 [{kind}] "
                     f"{it.get('title', '')[:34]}</li>")
    for it in fn[:3]:
        lines.append(f"<li style='color:#666'>　? {it.get('score')}分 "
                     f"{it.get('title', '')[:34]}（{it.get('length')}字）</li>")

    topics = proposal.get("research_topics", [])
    if topics:
        lines.append("<li><b>待检索主题</b>: " + "、".join(topics[:3]) + "</li>")
    return ("<hr><p><b>筛选机制自迭代（质量优先 · 宁缺毋滥）</b></p><ul>"
            + "".join(lines) + "</ul>")


def build_body(issues: list, pools: dict, proposal: dict = None) -> str:
    sev_cn = {"critical": "🔴 紧急", "warning": "🟡 注意"}
    rows = []
    for i in issues:
        rows.append(
            f"<h3>{sev_cn.get(i['severity'], '')} {i['title']}</h3>"
            f"<p>{i['detail']}</p>"
            f"<p><b>处理建议：</b>{i['fix']}</p>"
        )
    health_lines = []
    for key, _, label, _ in POOLS:
        p = pools.get(key, {})
        if p.get("present"):
            health_lines.append(
                f"{label}: {p['count']} 篇 / {p['n_sources']} 源, "
                f"正文 {p['with_content']} 篇, 更新于 {p['exported_at']} (约 {p['age_h']:.0f}h 前), "
                f"最新发布 {p['latest_pub_str']}")
        else:
            health_lines.append(f"{label}: <b>缺失/损坏</b> ({p.get('exported_at')})")
    return (
        f"<p><b>公众号日报链路 · 每日自查发现 {len(issues)} 项需关注</b></p>"
        + "".join(rows)
        + "<hr><p><b>当前三池快照：</b></p><ul>"
        + "".join(f"<li>{l}</li>" for l in health_lines)
        + "</ul><p style='color:#888'>本邮件由 self_check 自动发出；自愈项已自动处理，需你操作的项见上方建议。</p>"
    )


def main():
    dry_run = "--dry-run" in sys.argv
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    now = datetime.now(CST)
    run_date = now.strftime("%Y-%m-%d")
    print(f"== 链路自查自迭代(WeRSS 架构) · {run_date} (北京时间) ==")

    # 三池健康快照(任一缺失/损坏都不崩溃, 相应项单独告警)
    pools = compute_pools(now)
    for key, _, label, _ in POOLS:
        p = pools.get(key, {})
        print(f"  [{label}] present={p.get('present')} count={p.get('count')} "
              f"src={p.get('n_sources')} age_h={p.get('age_h')} "
              f"content={p.get('with_content')}/{p.get('count')}")

    hist_path = BASE_DIR / "data" / "sent_history.json"
    sent = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {}

    # 是否"本机今天未开机/未同步": 以 werss 微信池距上次推送时间判断
    werss = pools.get("werss")
    gap_h = werss["age_h"] if (werss and werss["present"]) else 0.0
    deferred = (werss and werss["present"]) and gap_h > WERSS_DEFER_H

    digest_run = get_latest_digest_run_today(run_date, TOKEN)
    issues, actions = classify(pools, sent, run_date, now, digest_run, deferred)

    # 若 digest 今天已发过异常告警(根因类), 避免与它的告警重复
    if f"__anomaly__{run_date}" in sent:
        issues = [i for i in issues if i["code"] not in ("werss_content",)]

    # 质量自迭代(三池扫描 + 回归 + 双向扫描 + 检索词生成; 仅产出提案文件)
    try:
        proposal = quality_iterate(pools, cfg)
        reg = proposal.get("regression", {})
        print(f"[+] 质量自迭代: 回归 {reg.get('passed', '?')}/{reg.get('total', '?')} 通过 | "
              f"疑似误杀 {len(proposal.get('suspected_false_negatives', []))} 条 | "
              f"疑似漏网 {len(proposal.get('suspected_false_positives', []))} 条")
    except Exception as e:
        print(f"[warn] 质量自迭代失败: {e}")
        proposal, reg = {}, {}

    # 质量红线: 回归不过 = 规则退化
    if reg.get("status") == "fail":
        detail = "；".join(
            f"《{f.get('title', '')[:22]}》期望{f.get('expect')}实为{f.get('actual')}"
            for f in reg.get("failed", [])[:3])
        issues.insert(0, {
            "code": "quality_regression", "severity": "critical",
            "title": "文章筛选规则退化(回归测试未通过)",
            "detail": f"回归用例 {reg['passed']}/{reg['total']} 通过, 失败: {detail}。"
                      f"说明最近的规则调整引入了回退 —— 要么放进了低质文, 要么误杀了好文。",
            "fix": "质量红线。需回滚或修正规则后重跑回归(命令: python quality_iteration.py)。"
                   f"我不会自动放宽门槛来让回归通过 —— 那违背宁缺毋滥。",
            "subject": "【紧急·质量回退】筛选规则回归测试未通过",
        })

    # 质量门槛被放宽
    for w in proposal.get("quality_priority_warnings", []):
        issues.append({
            "code": "quality_threshold", "severity": "warning",
            "title": "质量门槛被放宽",
            "detail": w,
            "fix": "宁缺毋滥: 无高质量文章时应少推甚至空刊, 而不是降低门槛凑数。请确认这是有意为之。",
            "subject": "【注意·质量门槛】筛选阈值被调低",
        })

    # 健康报告(始终写, 供后续复盘)
    report = {
        "run_date": run_date,
        "checked_at": now.isoformat(timespec="seconds"),
        "deferred": deferred,
        "hours_since_werss_sync": round(gap_h, 1),
        "issues": [i["code"] for i in issues],
        "actions": actions,
        "pools": {k: {kk: vv for kk, vv in v.items()
                      if kk not in ("label", "kind", "exported_dt", "latest_pub_dt")}
                  for k, v in pools.items()},
    }
    (BASE_DIR / "data" / "health_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    # 处置
    if dry_run:
        print(f"[dry-run] 发现 {len(issues)} 项需关注(不发送邮件):")
        for i in issues:
            print(f"   - [{i['severity']}] {i['code']}: {i['title']}")
        if actions:
            print("[dry-run] 自愈动作:", actions)
        print("SELF_CHECK_DONE")
        return

    if issues:
        subjects = [i["subject"] for i in issues][:3]
        body = build_body(issues, pools, proposal)
        digest_cloud.send_anomaly_burst(cfg, run_date, subjects, body, sent, hist_path)
    elif actions:
        info = "<p>公众号日报链路自查完成。发现 digest 今日未运行，已自动触发补跑（无需你操作）。</p>"
        if actions:
            info += "<ul>" + "".join(f"<li>{a}</li>" for a in actions) + "</ul>"
        digest_cloud.send_mail(f"公众号日报 · {run_date} · 自查已自动补跑", info, cfg)
        print("[+] 已发送'自动补跑'通知")
    else:
        if deferred:
            print(f"[+] 链路自查顺延: 今日本机未同步(WeRSS 距上次约 {gap_h:.0f}h, 可能电脑未开机), "
                  f"已跳过微信池新鲜度类误报、未发现问题; 明日继续巡检。")
        else:
            wp = pools.get("werss", {})
            print(f"[+] 链路健康, 无异常 (微信池 {wp.get('count')} 篇/{wp.get('n_sources')} 源, "
                  f"更新于 {wp.get('exported_at')}; RSS/Web 云端池正常)")

    print("SELF_CHECK_DONE")


if __name__ == "__main__":
    main()
