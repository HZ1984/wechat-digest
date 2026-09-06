#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文章筛选机制 · 每日自更新自迭代
===============================
设计原则(用户 2026-09-06 明确):
    质量永远第一, 行业权重第二。宁缺毋滥 —— 无高质量文章时宁可少推, 也不降低质量门槛。

本模块被 self_check.py 调用, 每日随自查运行, 完成四件事:

  1. 回归测试   : 重放 data/quality_regression.json 的历史样本, 断言规则未退化。
                  任何一条不过 = 质量回退 = 立即告警。这是"质量不倒退"的硬闸门。
  2. 双向扫描   : 误杀扫描(篇幅足却被过滤) + 漏网扫描(高分却疑似低质)。
                  两边都看, 避免只往一个方向调参导致规则失衡。
  3. 质量优先校验: 结构性断言 —— 质量门槛未被放宽(如 min_chars 被调低、top_n 被放大凑数)。
  4. 检索词生成 : 基于当日发现的问题类型, 产出次日需要检索的"成熟方案"关键词,
                  供 Agent(我)每日联网检索后迭代规则。

产出:
  data/quality_proposal.json  提案(仅提案, 绝不自动改 digest_cloud.py 主规则)
  data/quality_trend.json     逐日质量指标, 用于观察规则迭代的长期效果

依赖: 仅标准库; 复用 digest_cloud 的评分/过滤函数。
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))

# 三个源池: 微信(WeWe RSS) / RSS(官网 feed) / 官网直抓
POOLS = [
    ("微信", "data/articles_recent.json"),
    ("RSS", "data/rss_articles.json"),
    ("官网", "data/web_articles.json"),
]

# ---------------------------------------------------------------- 漏网扫描信号
# 设计要点: 单个弱信号不足以下结论(会误报刷屏), 需 >=2 个弱信号 或 1 个强信号。
# 这些是"疑似"清单, 供人确认, 不自动拦截 —— 避免误杀正常报道。
PR_TONE = [  # 宣传/通稿腔
    "隆重召开", "圆满落幕", "圆满成功", "高度评价", "纷纷表示", "莅临指导",
    "重要讲话", "深入学习贯彻", "贯彻落实", "凝心聚力", "砥砺前行", "携手共进",
    "再上新台阶", "开启新篇章", "谱写新篇章", "具有重要意义", "标志性成果",
]
CLICKBAIT = [  # 标题党
    "震惊", "必看", "万万没想到", "太突然", "竟然", "速看", "赶紧看",
    "刚刚传出", "彻底沸腾", "炸了", "慌了", "出大事",
]
RESIDUAL_AD = [  # 营销残留(抓取层漏过的)
    "加微信", "扫码咨询", "限时优惠", "优惠券", "点击购买", "立即抢购",
    "私信回复", "领取资料", "免费领取", "扫码进群",
]
STRONG_JUNK = ["隆重召开", "圆满落幕", "莅临指导", "扫码进群", "加微信"]

# 合集/栏目稿: 多条要闻拼接(早报/快讯/午报/要闻), 信息量大但无单一主线,
# 做"精读摘要"会散乱。2026-09-06 实测: 《早报 | 库克致全员信; 梅西退役; 华为营收…》
# 以 96 分排到 Top 4, 与"精读"定位不符, 故纳入可疑信号交人工裁决。
ROUNDUP_RE = [
    r"^\s*(早报|晚报|午报|晨报|快讯|简讯|要闻|每日速递|资讯|情报)",
    r"^[^｜|]{0,10}(早报|晚报|快讯)\s*[｜|]",
    r"[｜|].{0,40}[；;].{0,40}[｜|]",   # 多条目并列
]
ROUNDUP_STRONG = ["早报", "晚报", "快讯", "每日速递"]


# ---------------------------------------------------------------- 数据加载
def load_all_pools(base_dir: Path) -> dict:
    """加载三个源池, 返回 {pool_name: [articles]}。缺失的池静默跳过。"""
    pools = {}
    for name, rel in POOLS:
        fp = base_dir / rel
        if not fp.exists():
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            arts = data.get("articles", [])
            for a in arts:
                a["_pool"] = name
            pools[name] = arts
        except Exception as e:
            print(f"  [warn] 读取 {rel} 失败: {e}")
    return pools


def merge_pools(pools: dict) -> list:
    out = []
    for arts in pools.values():
        out.extend(arts)
    return out


def _pub_date(a: dict):
    """解析文章日期, 失败返回 None。兼容 pub_date(字符串) 与 publish_time(时间戳)。"""
    s = a.get("pub_date") or ""
    if s:
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d")
        except Exception:
            pass
    pt = a.get("publish_time")
    if pt:
        try:
            pt = float(pt)
            # 该库历史上有秒级/毫秒级混用, 按量级判断
            return datetime.fromtimestamp(pt / 1000 if pt > 1e11 else pt)
        except Exception:
            pass
    return None


def in_window(a: dict, cfg: dict, now: datetime) -> bool:
    """是否在回溯窗口内。

    关键: 只有窗口内的文章才可能是"误杀"。窗口外的旧文是被 lookback_days 正常淘汰的,
    若混进误杀清单会把每日迭代方向带偏(2026-09-06 实测: 11 条"误杀"全是 6~9 天前的旧文)。
    """
    d = _pub_date(a)
    if d is None:
        return False
    days = cfg.get("lookback_days", 7)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.date() >= start.date()


# ---------------------------------------------------------------- 1. 回归测试
def run_regression(base_dir: Path, cfg: dict) -> dict:
    """重放历史样本, 断言规则未退化。返回 {total, passed, failed:[...]}。"""
    import digest_cloud as D

    fp = base_dir / "data" / "quality_regression.json"
    if not fp.exists():
        return {"status": "no_file", "total": 0, "passed": 0, "failed": []}

    data = json.loads(fp.read_text(encoding="utf-8"))
    rules = {**D.DEFAULT_QUALITY_RULES, **cfg.get("quality_rules", {})}
    results, failed = [], []

    for c in data.get("cases", []):
        art = {
            "title": c.get("title", ""),
            "source": c.get("source", ""),
            "text": c.get("text_head", ""),
            "content_html": "",
            "pub_date": c.get("pub_date", ""),
            "link": "",
        }
        # 判定链条与 digest_cloud 的筛选顺序保持一致
        try:
            art["clean_text"] = D.clean_text(art["text"], rules)
            low = D.is_low_value(art, rules)
            ev = D.is_event_recruit(art, rules)
            cr = D.is_corp_report(art, rules) if hasattr(D, "is_corp_report") else (False, "")
        except Exception as e:
            failed.append({"id": c.get("id"), "title": c.get("title", "")[:40],
                           "expect": c.get("expect"), "actual": "error",
                           "reason": f"判定异常: {e}"})
            continue

        rejected = bool(low or ev[0] or cr[0])
        why = (ev[1] or cr[1] or ("低信息密度" if low else ""))
        actual = "reject" if rejected else "keep"
        ok = (actual == c.get("expect"))
        rec = {"id": c.get("id"), "title": c.get("title", "")[:50],
               "source": c.get("source", ""), "expect": c.get("expect"),
               "actual": actual, "reason": why[:80], "ok": ok}
        results.append(rec)
        if not ok:
            failed.append(rec)

    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    return {
        "status": "pass" if not failed else "fail",
        "total": total, "passed": passed, "failed": failed,
        "pass_rate": round(passed / total * 100, 1) if total else 0.0,
    }


# ---------------------------------------------------------------- 2a. 误杀扫描
def scan_false_negative(articles: list, cfg: dict, now: datetime, limit: int = 8) -> list:
    """篇幅充足、非营销、且在回溯窗口内, 却被质量规则判低信息密度 -> 疑似误杀。

    注意: 必须限定在回溯窗口内。窗口外的旧文属于被 lookback_days 正常淘汰,
    不是质量规则的问题, 混入会误导迭代方向。
    """
    import digest_cloud as D

    rules = {**D.DEFAULT_QUALITY_RULES, **cfg.get("quality_rules", {})}
    blacklist = set(cfg.get("source_blacklist", []))
    out = []
    for a in articles:
        if a.get("source") in blacklist:
            continue
        if not in_window(a, cfg, now):       # 窗口外 = 正常淘汰, 不算误杀
            continue
        raw = a.get("text", "") or ""
        if not raw:
            continue
        try:
            a["clean_text"] = D.clean_text(raw, rules)
            if not D.is_low_value(a, rules):
                continue
            ev = D.heuristic_score(a, cfg)
        except Exception:
            continue
        length = len(a.get("clean_text") or raw)
        is_mkt = ev.get("flags", {}).get("marketing")
        if length >= 2500 and not is_mkt:
            out.append({
                "title": a.get("title", "")[:60], "source": a.get("source", ""),
                "pool": a.get("_pool", ""), "length": length,
                "score": round(ev.get("score", 0), 1),
                "reasons": ev.get("reasons", [])[:3],
            })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]


# ---------------------------------------------------------------- 2b. 漏网扫描
def scan_false_positive(articles: list, cfg: dict, now: datetime,
                        min_score: float = 55.0, limit: int = 8) -> list:
    """高分候选里疑似低质的: 宣传通稿 / 标题党 / 营销残留 / 空泛无数据。

    min_score 门槛的意义: 只关注"真有可能进榜"的文章。低分文章即使命中信号也不会被选中,
    报出来只是噪音(2026-09-06 实测: 不加门槛时全是 22~50 分的时政通稿, 无行动价值)。
    """
    import digest_cloud as D

    rules = {**D.DEFAULT_QUALITY_RULES, **cfg.get("quality_rules", {})}
    out = []
    for a in articles:
        raw = a.get("text", "") or ""
        if len(raw) < 200:
            continue
        if not in_window(a, cfg, now):       # 同样只关心窗口内
            continue
        try:
            a["clean_text"] = D.clean_text(raw, rules)
            if D.is_low_value(a, rules):
                continue
            if D.is_event_recruit(a, rules)[0]:
                continue
            if hasattr(D, "is_corp_report") and D.is_corp_report(a, rules)[0]:
                continue
            ev = D.heuristic_score(a, cfg)
        except Exception:
            continue

        score = round(ev.get("score", 0), 1)
        if score < min_score:                 # 进不了榜的不报, 避免噪音
            continue

        title = a.get("title", "") or ""
        head = (a.get("clean_text") or raw)[:1500]
        sig = {
            "宣传通稿腔": [w for w in PR_TONE if w in head or w in title],
            "标题党": [w for w in CLICKBAIT if w in title],
            "营销残留": [w for w in RESIDUAL_AD if w in head],
            "合集栏目稿": [f"命中「{m.group(0)[:16]}」" for m in
                          (re.search(p, title) for p in ROUNDUP_RE) if m],
        }
        # 空泛: 正文有一定长度却几乎无数字(缺少数据支撑)
        digits = len(re.findall(r"\d", head))
        vague = len(head) >= 500 and digits < len(head) * 0.008
        if vague:
            sig["空泛无数据"] = [f"正文{len(head)}字仅{digits}个数字"]

        n_weak = sum(1 for v in sig.values() if v)
        has_strong = (any(w in head or w in title for w in STRONG_JUNK)
                      or any(w in title for w in ROUNDUP_STRONG))
        if n_weak >= 2 or has_strong:
            out.append({
                "title": title[:60], "source": a.get("source", ""),
                "pool": a.get("_pool", ""), "score": score,
                "signals": {k: v[:4] for k, v in sig.items() if v},
                "signal_count": n_weak, "strong": has_strong,
            })
    out.sort(key=lambda x: (-x["score"], -x["signal_count"]))
    return out[:limit]


# ---------------------------------------------------------------- 3. 质量优先校验
def check_quality_priority(cfg: dict) -> list:
    """结构性断言: 质量门槛不得被放宽, 行业权重不得盖过质量。"""
    warns = []
    min_chars = cfg.get("min_chars", 1500)
    if min_chars < 1000:
        warns.append(f"min_chars 已降到 {min_chars}, 低于 1000 会让短文大量混入, 违背宁缺毋滥")

    top_n = cfg.get("top_n", 8)
    if top_n and top_n > 12:
        warns.append(f"top_n = {top_n}, 超过 12 会为凑数降低单篇质量")

    qr = cfg.get("quality_rules", {})
    required = ["event_cta_re", "event_title_words", "event_body_words", "corp_report_re"]
    missing = [k for k in required if not qr.get(k)]
    if missing:
        warns.append(f"质量规则关键项缺失: {missing} —— 活动帖/企业宣传稿将漏网")

    lb = cfg.get("lookback_days", 3)
    if lb > 10:
        warns.append(f"lookback_days = {lb}, 回溯过久会让旧文挤占新文位置(建议 3~7)")

    # 行业权重不得为负向惩罚(即不得因不属于四大兴趣域而重度扣分)
    return warns


# ---------------------------------------------------------------- 4. 检索词生成
def gen_research_topics(reg: dict, fn: list, fp: list) -> list:
    """基于当日发现的问题类型, 产出需要联网检索的关键词(供 Agent 次日迭代)。"""
    topics = []
    if reg.get("status") == "fail":
        topics.append("内容质量过滤 规则退化 回归测试 方法")
    if fn:
        topics.append("低信息密度内容 误杀 判定 阈值 内容策展")
    if fp:
        kinds = {k for it in fp for k in it.get("signals", {})}
        if "宣传通稿腔" in kinds:
            topics.append("企业宣传稿 通稿 自动识别 NLP 特征")
        if "标题党" in kinds:
            topics.append("标题党 clickbait 检测 中文 2026")
        if "空泛无数据" in kinds:
            topics.append("内容信息密度 评估 指标 信息熵")
        if "营销残留" in kinds:
            topics.append("软文 广告 识别 规则 内容平台")
        if "合集栏目稿" in kinds:
            topics.append("newsletter 选题 单篇深度 vs 资讯合集 精读摘要")
    if not topics:
        topics.append("内容质量评分 体系 最佳实践 newsletter curation")
    return topics[:5]


# ---------------------------------------------------------------- 趋势记录
def update_trend(base_dir: Path, metrics: dict):
    fp = base_dir / "data" / "quality_trend.json"
    hist = []
    if fp.exists():
        try:
            hist = json.loads(fp.read_text(encoding="utf-8"))
            if not isinstance(hist, list):
                hist = []
        except Exception:
            hist = []
    hist.append(metrics)
    hist = hist[-60:]  # 保留 60 天
    fp.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- 主入口
def run(base_dir: Path, cfg: dict) -> dict:
    """返回 proposal dict; 同时写 data/quality_proposal.json 与 quality_trend.json。"""
    import digest_cloud as D

    now = datetime.now(CST)
    print("\n== 筛选机制自迭代 ==")

    pools = load_all_pools(base_dir)
    print(f"  源池: " + ", ".join(f"{k}({len(v)})" for k, v in pools.items()) or "  无数据")
    all_a = merge_pools(pools)

    # 1. 回归
    reg = run_regression(base_dir, cfg)
    icon = "✓" if reg.get("status") == "pass" else ("✗" if reg.get("status") == "fail" else "-")
    print(f"  {icon} 回归测试: {reg.get('passed','?')}/{reg.get('total','?')} 通过 "
          f"({reg.get('pass_rate','?')}%)")
    for f in reg.get("failed", [])[:5]:
        print(f"      ✗ [{f.get('expect')}→{f.get('actual')}] {f.get('title','')[:36]} | {f.get('reason','')[:40]}")

    # 2. 双向扫描(均限定在回溯窗口内, 窗口外属正常淘汰)
    fn = scan_false_negative(all_a, cfg, now)
    fp_ = scan_false_positive(all_a, cfg, now)
    print(f"  · 疑似误杀(被过滤但篇幅足): {len(fn)} 条")
    print(f"  · 疑似漏网(高分但可疑低质): {len(fp_)} 条")
    for it in fp_[:5]:
        kd = "/".join(it["signals"].keys())
        print(f"      ! {it['score']:5.1f}分 [{kd}] {it['title'][:38]}")

    # 3. 质量优先校验
    warns = check_quality_priority(cfg)
    if warns:
        print(f"  ⚠ 质量优先校验: {len(warns)} 项告警")
        for w in warns:
            print(f"      - {w}")
    else:
        print("  ✓ 质量优先校验通过(门槛未放宽)")

    # 4. 检索词
    topics = gen_research_topics(reg, fn, fp_)
    print(f"  · 次日待检索主题: {len(topics)} 条")

    proposal = {
        "generated_at": now.isoformat(timespec="seconds"),
        "principle": "质量永远第一, 行业权重第二; 宁缺毋滥。本文件为提案, 不自动应用。",
        "regression": reg,
        "suspected_false_negatives": fn,
        "suspected_false_positives": fp_,
        "quality_priority_warnings": warns,
        "research_topics": topics,
        "pool_sizes": {k: len(v) for k, v in pools.items()},
        "note": "确认后由 Agent 修改 digest_cloud.py 主规则, 并追加用例到 quality_regression.json 后跑回归。",
    }
    out = base_dir / "data" / "quality_proposal.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(proposal, ensure_ascii=False, indent=1), encoding="utf-8")

    update_trend(base_dir, {
        "date": now.strftime("%Y-%m-%d"),
        "regression_pass_rate": reg.get("pass_rate"),
        "regression_status": reg.get("status"),
        "suspected_fn": len(fn),
        "suspected_fp": len(fp_),
        "quality_warnings": len(warns),
        "pool_sizes": {k: len(v) for k, v in pools.items()},
        "research_topics": topics,
    })
    return proposal


if __name__ == "__main__":
    from pathlib import Path as P
    base = P(__file__).resolve().parent
    cfg = json.loads((base / "config.json").read_text(encoding="utf-8"))
    run(base, cfg)
