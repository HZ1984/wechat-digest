#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公众号每日精选 · 云端版(GitHub Actions 运行)
============================================
读取 data/articles_recent.json(由本地 sync_data.py 推送) ->
  按天去重(sent_history) -> 启发式评分 -> 主线+自由探索精选 ->
  生成邮件 HTML -> SMTP 发送到收件箱 -> 更新 sent_history(由 workflow 提交回仓库)

依赖: 仅 Python 标准库
环境变量:
  SMTP_USER  发件邮箱(如 xxx@qq.com)
  SMTP_PASS  SMTP 授权码(QQ邮箱设置中生成)
  TO_EMAIL   收件邮箱(可选, 默认取 config.json 的 to_email)
"""

import html
import json
import os
import random
import re
import smtplib
import sys
import xml.etree.ElementTree as ET  # noqa: F401 (保持与本地版一致性)
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

CST = timezone(timedelta(hours=8))  # 东八区
BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------- 评分规则(与本地 digest.py 保持一致)
# 营销词分级: 强营销词(标题命中=一票否决, 正文命中只轻扣分) / 弱营销词(标题命中扣分, 正文不扣)
# 背景: 旧版用单一词表且"标题或正文命中即排除", 导致"广告/清仓/下单"等正常用词误杀大量好文章
STRONG_MARKETING = [
    "免费领取", "限时", "秒杀", "抢购", "抽奖", "转发到朋友圈", "点击下方链接",
    "扫码领取", "促销", "优惠券", "买一送一", "双11", "618", "清仓",
    "最后一天", "速抢", "拼团", "砍价", "邀请好友", "红包", "福利社",
    "点击购买", "下单", "立即购买", "免费领取", "领券",
]
WEAK_MARKETING = ["广告", "推广", "软文", "合作推广", "商务合作", "补贴", "折扣", "赞助"]

# 低信息密度内容: 标题命中即一票否决(公告/日历/预警/期刊推荐等短讯栏目)
LOW_VALUE_KEYWORDS = [
    "迎日历", "日历", "答记者问", "公告", "声明", "预警", "快讯", "简报",
    "直播预告", "开奖", "中奖", "招聘", "征稿", "报名", "重磅推荐", "期刊推荐",
    "直播开讲", "有奖", "征订",
]

CLICKBAIT_WORDS = [
    "震惊", "重磅", "万万没想到", "不看后悔", "内部消息", "独家揭秘", "惊天",
    "吓尿", "哭晕", "沸腾了", "炸锅", "疯了", "不可思议", "罕见", "刚刚",
    "突发", "紧急", "马上删", "快看", "出大事", "彻底", "绝了", "必看",
]

AD_ZONE_RE = re.compile(r"广告|商务合作|软文")
AD_CTA_RE = re.compile(r"点击|购买|下单|扫码|咨询|添加微信|优惠|电话")


def is_ad_zone(text: str) -> bool:
    """检测正文前 1500 字内是否夹带硬广(营销词与引导词相距 100 字内)"""
    head = text[:1500]
    for m in AD_ZONE_RE.finditer(head):
        if AD_CTA_RE.search(head[max(0, m.start() - 100):m.start() + 80]):
            return True
    return False


def is_low_value(article: dict) -> bool:
    """标题或正文开头命中低信息密度关键词(公告/日历/预警等)"""
    title = article.get("title", "")
    head = article.get("text", "")[:300]
    return any(k in title or k in head for k in LOW_VALUE_KEYWORDS)


def interest_hit(kw: str, text_lower: str):
    """兴趣匹配: 纯英文/数字关键词要求词边界, 避免 'ai' 误匹配 'said' 等"""
    if re.fullmatch(r"[a-z0-9]+", kw.lower()):
        return re.search(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])", text_lower)
    return kw.lower() in text_lower


def marketing_penalty(title: str, text: str) -> tuple:
    """返回 (score_delta, is_marketing, reason)。仅标题强营销词/硬广区=排除, 正文弱词不误杀"""
    head = text[:1500]
    strong_title = [w for w in STRONG_MARKETING if w in title]
    strong_head = [w for w in STRONG_MARKETING if w in head]
    weak_title = [w for w in WEAK_MARKETING if w in title]

    if strong_title or len(weak_title) >= 2:
        words = (strong_title or weak_title)[:2]
        return -30, True, f"标题含营销词({', '.join(words)})"
    if weak_title:
        return -8, False, f"标题提及弱营销词({weak_title[0]})"
    delta, reasons = 0, []
    if strong_head:
        delta -= min(len(strong_head) * 4, 12)
        reasons.append(f"正文提及营销词({', '.join(strong_head[:2])})")
    if is_ad_zone(text):
        return delta - 30, True, "正文夹带硬广区域"
    return delta, False, "; ".join(reasons)

SKIP_KEYWORDS = ["在小说阅读器", "去阅读", "沉浸阅读", "Original",
                 "The following article is From", "微信扫一扫关注该公众号",
                 "预览时标签不可点", "收录于合集", "个相关内容"]


def count_tags(content_html: str, tag: str) -> int:
    return len(re.findall(rf"<{tag}[\s>]", content_html, flags=re.I))


def heuristic_score(article: dict, cfg: dict) -> dict:
    title, text = article["title"], article["text"]
    content_html = article["content_html"]
    score, reasons = 50.0, []

    chars = len(text)
    if chars >= 8000:
        score += 20; reasons.append(f"正文 {chars} 字，内容充实")
    elif chars >= 4000:
        score += 14; reasons.append(f"正文 {chars} 字，篇幅可观")
    elif chars >= 2000:
        score += 8; reasons.append(f"正文 {chars} 字")
    elif chars < cfg.get("min_chars", 1500):
        score -= 15; reasons.append(f"正文仅 {chars} 字，偏短")

    h_count = sum(count_tags(content_html, f"h{i}") for i in range(2, 5))
    code_blocks = len(re.findall(r"<pre[\s>]|<code[\s>]", content_html, flags=re.I))
    tables = count_tags(content_html, "table")
    structure_bonus = min(h_count * 2, 8) + min(code_blocks * 3, 9) + min(tables * 2, 6)
    score += structure_bonus
    if structure_bonus >= 10:
        reasons.append(f"结构丰富(标题 {h_count}/代码 {code_blocks}/表格 {tables})")

    interests = [kw.lower() for kw in cfg.get("interests", []) if kw]
    if interests:
        text_lower = (title + " " + text[:3000]).lower()
        hits = [kw for kw in interests if interest_hit(kw, text_lower)]
        if hits:
            score += min(len(hits) * 4, 12)
            reasons.append(f"命中兴趣: {'、'.join(hits[:3])}")

    m_delta, m_flag, m_reason = marketing_penalty(title, text)
    score += m_delta
    if m_reason:
        reasons.append(m_reason)

    clickbait_hits = [w for w in CLICKBAIT_WORDS if w in title]
    if clickbait_hits:
        score -= min(len(clickbait_hits) * 6, 18)
        reasons.append(f"标题党({', '.join(clickbait_hits[:2])})")

    account = article.get("source", "")
    if account in cfg.get("account_weights", {}):
        score += cfg["account_weights"][account]
        reasons.append(f"来源「{account}」加权")

    return {"score": round(max(0, min(score, 100)), 1), "reasons": reasons,
            "flags": {"marketing": m_flag, "clickbait": bool(clickbait_hits)}}


def explore_score(article: dict, cfg: dict, rng) -> dict:
    title, text = article["title"], article["text"]
    content_html = article["content_html"]
    score, reasons = 40.0, []

    chars = len(text)
    min_explore = cfg.get("explore_min_chars", 1200)
    if chars >= 8000:
        score += 20
    elif chars >= 4000:
        score += 14
    elif chars >= 2000:
        score += 8
    elif chars < min_explore:
        score -= 20; reasons.append(f"正文仅 {chars} 字，偏短")

    h_count = sum(count_tags(content_html, f"h{i}") for i in range(2, 5))
    code_blocks = len(re.findall(r"<pre[\s>]|<code[\s>]", content_html, flags=re.I))
    tables = count_tags(content_html, "table")
    score += min(h_count * 2, 8) + min(code_blocks * 3, 9) + min(tables * 2, 6)

    interests = [kw.lower() for kw in cfg.get("interests", []) if kw]
    if interests:
        text_lower = (title + " " + text[:2000]).lower()
        hits = [kw for kw in interests if interest_hit(kw, text_lower)]
        if hits:
            score -= min(len(hits) * 5, 20)

    m_delta, m_flag, m_reason = marketing_penalty(title, text)
    score += m_delta
    if m_reason:
        reasons.append(m_reason)
    clickbait_hits = [w for w in CLICKBAIT_WORDS if w in title]
    if clickbait_hits:
        score -= min(len(clickbait_hits) * 6, 18)

    score += rng.uniform(0, cfg.get("explore_jitter", 6))
    if not reasons:
        reasons.append("自由探索 · 跳出既定兴趣圈")
    return {"score": round(max(0, min(score, 100)), 1), "reasons": reasons,
            "flags": {"marketing": m_flag, "clickbait": bool(clickbait_hits)}}


# ---------------------------------------------------------------- 邮件 HTML(内联样式, 兼容各邮件客户端)
def clean_digest(text: str, title: str) -> str:
    t = html.unescape(text)
    lines = [l.strip() for l in t.split("\n") if l.strip()]
    kept = [l for l in lines if not any(k in l for k in SKIP_KEYWORDS)]
    s = " ".join(kept)
    if title and s.startswith(title):
        s = s[len(title):].strip()
    return s[:240] + ("…" if len(s) > 240 else "")


def gen_email_html(selected: list, run_date: str, cfg: dict, stale_note: str = "") -> str:
    e = html.escape
    n_explore = sum(1 for a in selected if a.get("type") == "explore")
    rows = []
    for i, a in enumerate(selected, 1):
        digest = clean_digest(a["text"], a["title"])
        reason = "；".join(a["eval"]["reasons"][:3]) or "综合质量较好"
        is_explore = a.get("type") == "explore"
        badge = (' <span style="color:#534ab7;background:#eeedfe;padding:1px 8px;'
                 'border-radius:8px;margin-left:6px;font-weight:400;">自由探索</span>') if is_explore else ""
        border = "border-left:3px solid #7f77dd;" if is_explore else ""
        rows.append(f'''
<tr><td style="padding:16px 20px;border-bottom:1px solid #eee;{border}">
  <div style="font-size:12px;color:#993c1d;font-weight:600;margin-bottom:4px;">#{i}
    <span style="color:#0f6e56;background:#e1f5ee;padding:1px 8px;border-radius:8px;margin-left:6px;font-weight:400;">{a['eval']['score']:.0f} 分</span>{badge}
    <span style="color:#888;margin-left:6px;font-weight:400;">{e(a['source'])}</span></div>
  <a href="{a['link']}" style="font-size:16px;font-weight:600;color:#0c447c;text-decoration:none;">{e(a['title'])}</a>
  <p style="font-size:13px;color:#555;margin:8px 0 6px;line-height:1.6;">{e(digest)}</p>
  <div style="font-size:12px;color:#999;"><span style="background:#faeeda;color:#854f0b;padding:1px 8px;border-radius:8px;margin-right:6px;">推荐理由</span>{e(reason)}</div>
</td></tr>''')

    n_sources = cfg.get("n_sources", 18)
    explore_note = f" · 含 {n_explore} 篇自由探索" if n_explore else ""
    stale_html = (f'<div style="margin-top:8px;font-size:12px;color:#b45309;">{e(stale_note)}</div>'
                  if stale_note else "")
    interests = "、".join(cfg.get("interests", [])[:8])
    return f'''<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f5f4f0;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;">
<div style="max-width:680px;margin:0 auto;padding:20px 12px;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid #e5e3da;border-radius:12px;margin-bottom:14px;">
<tr><td style="padding:24px 28px;">
  <h1 style="margin:0 0 6px;font-size:22px;font-weight:600;color:#2c2c2a;">公众号精选日报</h1>
  <div style="color:#888;font-size:13px;">{run_date} · 从 {n_sources} 个公众号中精选 {len(selected)} 篇{explore_note}</div>
  <div style="margin-top:8px;font-size:12px;color:#185fa5;">兴趣方向: {e(interests)}</div>
  {stale_html}
</td></tr>
</table>
<table width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid #e5e3da;border-radius:12px;">{''.join(rows)}
</table>
<div style="text-align:center;color:#aaa;font-size:12px;margin-top:16px;">由 GitHub Actions 云端自动生成并发送 · 电脑关机也照常送达</div>
</div>
</body></html>'''


# ---------------------------------------------------------------- 邮件发送
def send_mail(subject: str, html_body: str, cfg: dict):
    if "--dry-run" in sys.argv:
        print(f"[dry-run] 跳过发信: {subject} ({len(html_body)} 字符)")
        return
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    to_email = os.environ.get("TO_EMAIL", "") or cfg.get("to_email", "")
    if not smtp_user or not smtp_pass:
        print("[error] 缺少 SMTP_USER / SMTP_PASS 环境变量(请在仓库 Secrets 配置)")
        sys.exit(1)

    host = cfg.get("smtp_host", "smtp.qq.com")
    port = cfg.get("smtp_port", 465)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("公众号精选日报", "utf-8")), smtp_user))
    msg["To"] = to_email
    msg.attach(MIMEText("请使用支持 HTML 的邮件客户端查看本邮件", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL(host, port, timeout=30) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_email], msg.as_string())
    print(f"[+] 邮件已发送 -> {to_email}")


# ---------------------------------------------------------------- 主流程
def main():
    dry_run = "--dry-run" in sys.argv
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    now = datetime.now(CST)
    run_date = now.strftime("%Y-%m-%d")
    print(f"== 云端精选管线 · {run_date} (北京时间) ==")

    data_path = BASE_DIR / "data" / "articles_recent.json"
    if not data_path.exists():
        print("[error] data/articles_recent.json 不存在, 请先在本地运行 sync_data.py")
        send_mail(f"公众号日报 · {run_date} · 数据缺失提醒",
                  f"<p>仓库中没有文章数据(data/articles_recent.json 缺失)。</p>"
                  f"<p>请在本地电脑运行 sync_data.py 同步数据。</p>", cfg)
        return
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    all_articles = payload.get("articles", [])
    exported_at = payload.get("exported_at", "unknown")
    cfg["n_sources"] = payload.get("n_sources", 18)
    print(f"[+] 数据: {len(all_articles)} 篇 (本地导出于 {exported_at})")

    # 发送历史(按天去重)
    hist_path = BASE_DIR / "data" / "sent_history.json"
    sent = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {}

    # 当天只发一封(防 GitHub schedule 多时间点重复触发, 也防手动+定时叠加)
    if not dry_run and run_date in sent.values():
        print(f"[skip] {run_date} 今天已发送过日报, 跳过本次(防止重复推送)")
        return

    lookback = now - timedelta(days=cfg.get("lookback_days", 3) - 1)
    lookback = lookback.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates, stale_note, skipped_low = [], [], 0
    for a in all_articles:
        try:
            pub = datetime.strptime(a["pub_date"][:10], "%Y-%m-%d")
        except Exception:
            continue
        if pub.date() < lookback.date():
            continue
        # 兼容三种历史键: 完整链接 / id:slug / title:标题 (与本地 digest.py 互通)
        slug = a["link"].split("/s/")[-1].split("?")[0].split("#")[0]
        if a["link"] in sent or ("id:" + slug) in sent or ("title:" + a["title"].strip()) in sent:
            continue
        if is_low_value(a):   # 低信息密度内容(公告/日历/预警/期刊推荐等)直接剔除
            skipped_low += 1
            continue
        candidates.append(a)
    print(f"[+] 回溯 {cfg.get('lookback_days', 3)} 天且未推送过: {len(candidates)} 篇"
          f" (另有 {skipped_low} 篇低信息密度内容已剔除)")

    # 无新文章: 发提示邮件(不中断, 保持系统可感知)
    if not candidates:
        body = (f"<p>今天没有新文章可推荐。</p>"
                f"<p>本地数据最后更新时间: {exported_at}(北京时间)。</p>"
                f"<p>若已超过一天, 说明抓取电脑最近没有开机, 开机后会自动恢复。</p>")
        send_mail(f"公众号日报 · {run_date} · 今日暂无新文章", body, cfg)
        print("[+] 已发送'暂无新文章'通知")
        return

    # 评分 + 精选(主线 + 自由探索, 与本地版同一套逻辑)
    for a in candidates:
        a["eval"] = heuristic_score(a, cfg)

    limit = cfg.get("max_results", 8)
    explore_count = min(cfg.get("explore_count", 2), limit // 2)
    main_count = limit - explore_count
    rng = random.Random(run_date)

    ranked = sorted(candidates, key=lambda a: a["eval"]["score"], reverse=True)
    # 主线: 非营销 + 字数达标(宁缺毋滥, 不补位短篇)
    min_chars = cfg.get("min_chars", 1500)
    main_pool = [a for a in ranked
                 if not a["eval"]["flags"].get("marketing")
                 and len(a["text"]) >= min_chars]
    main_selected = main_pool[:main_count]
    if len(main_pool) > main_count:
        print(f"[+] 主线候选 {len(main_pool)} 篇, 取前 {main_count}")
    elif len(main_pool) < main_count:
        print(f"[+] 主线达标仅 {len(main_pool)} 篇(<{main_count}), 宁缺毋滥不再补位短篇")
    for a in main_selected:
        a["type"] = "main"

    selected = list(main_selected)
    if explore_count > 0:
        chosen_links = {a["link"] for a in selected}
        picked_sources = {a["source"] for a in selected if a["source"]}
        min_explore = cfg.get("explore_min_chars", 1200)
        pool = [a for a in candidates
                if a["link"] not in chosen_links
                and not a["eval"]["flags"].get("marketing")
                and len(a["text"]) >= min_explore]
        for a in pool:
            a["explore_eval"] = explore_score(a, cfg, rng)
        explore_ranked = sorted(pool, key=lambda a: a["explore_eval"]["score"], reverse=True)
        for a in explore_ranked:  # 第一轮: 来源不重复
            if len(selected) >= limit:
                break
            if a["source"] and a["source"] in picked_sources:
                continue
            a["eval"] = a["explore_eval"]
            a["type"] = "explore"
            selected.append(a)
            chosen_links.add(a["link"])
            if a["source"]:
                picked_sources.add(a["source"])
        for a in explore_ranked:  # 第二轮: 放宽来源限制
            if len(selected) >= limit:
                break
            if a["link"] in chosen_links:
                continue
            a["eval"] = a["explore_eval"]
            a["type"] = "explore"
            selected.append(a)

    n_explore = sum(1 for a in selected if a.get("type") == "explore")
    if not selected:
        body = (f"<p>今天候选 {len(candidates)} 篇, 但通过质量门槛(正文≥{cfg.get('min_chars', 1500)}字、"
                f"非营销、非公告/日历/预警等低质内容)的为 0 篇, 因此不推送低质日报。</p>"
                f"<p>本地数据最后更新时间: {exported_at}(北京时间)。</p>"
                f"<p>通常第二天新文章增多后会自动恢复。</p>")
        send_mail(f"公众号日报 · {run_date} · 今日暂无高质量文章", body, cfg)
        print("[+] 已发送'暂无高质量文章'通知(宁缺毋滥)")
        return
    print(f"== 精选 Top {len(selected)} (主线 {len(selected) - n_explore} + 探索 {n_explore}) ==")
    for i, a in enumerate(selected, 1):
        tag = "[探索]" if a.get("type") == "explore" else ""
        print(f"  {i}. {tag}[{a['eval']['score']:.0f}分] {a['title']} ({a['source']})")

    # 数据新鲜度提示(本地超过 26 小时未同步时)
    try:
        exp_dt = datetime.fromisoformat(exported_at)
        if now - exp_dt > timedelta(hours=26):
            stale_note = f"注: 抓取电脑已 {(now - exp_dt).total_seconds() / 3600:.0f} 小时未同步, 今日精选基于稍早的数据"
    except Exception:
        pass

    # 生成邮件并归档
    email_html = gen_email_html(selected, run_date, cfg, stale_note)
    out_dir = BASE_DIR / "output"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"digest_{run_date}.html").write_text(email_html, encoding="utf-8")

    send_mail(f"公众号精选日报 · {run_date}", email_html, cfg)

    # 更新发送历史(保留 45 天)
    if dry_run:
        print("[dry-run] 跳过 sent_history 更新")
        return
    for a in selected:
        slug = a["link"].split("/s/")[-1].split("?")[0].split("#")[0]
        sent[a["link"]] = run_date
        sent["id:" + slug] = run_date
        sent["title:" + a["title"].strip()] = run_date
    cutoff = (now - timedelta(days=45)).strftime("%Y-%m-%d")
    sent = {k: v for k, v in sent.items() if v >= cutoff}
    hist_path.write_text(json.dumps(sent, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[+] sent_history 已更新(将由 workflow 提交回仓库)")


if __name__ == "__main__":
    main()
