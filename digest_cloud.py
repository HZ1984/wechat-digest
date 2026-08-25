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
    "直播预告", "开奖", "中奖", "招聘启事", "征稿", "报名", "重磅推荐", "期刊推荐",
    "直播开讲", "有奖", "征订",
]

CLICKBAIT_WORDS = [
    "震惊", "重磅", "万万没想到", "不看后悔", "内部消息", "独家揭秘", "惊天",
    "吓尿", "哭晕", "沸腾了", "炸锅", "疯了", "不可思议", "罕见", "刚刚",
    "突发", "紧急", "马上删", "快看", "出大事", "彻底", "绝了", "必看",
    "历史上第一次", "史上首次", "史上第一次", "人类首次", "里程碑式", "划时代",
]

AD_ZONE_RE = re.compile(r"广告|商务合作|软文")
# 8/25 修复: 去掉裸"点击"/"电话"——商业/科技分析常讨论"流量—点击—广告"变现链条、
# "广告业务收入"等术语(虎嗅《百度AI云增长50%》误杀案例), 只保留明确的购买/导流指令
AD_CTA_RE = re.compile(r"购买|下单|扫码|咨询|添加微信|优惠|立即抢购|抢购|点击购买|点击下方")


def is_ad_zone(text: str) -> bool:
    """检测正文前 1500 字内是否夹带硬广(营销词与引导词相距 100 字内)。
    8/25 修复: 排除媒体文末页脚模板——
      1) 广告位模板 "即刻购买（广告）"/"点击图片 即刻购买"
      2) 商务合作联系 "广告、商务合作：<电话/邮箱/微信>"
    否则南风窗等媒体的通用页脚会被当成硬广区域, 整站短文误杀"""
    head = text[:1500]
    for m in AD_ZONE_RE.finditer(head):
        pre = head[max(0, m.start() - 30):m.start()]
        post = head[m.end():m.end() + 40]
        if re.search(r"即刻购买|点击图片|广告位|广告投放", pre):
            continue  # 媒体广告位模板
        if re.search(r"、?\s*商务合作", post[:8]):
            continue  # 页脚"广告、商务合作：联系方式"
        if re.match(r"[:：]\s*[a-zA-Z0-9_@.+-]{4,}", post):
            continue  # 页脚"商务合作：电话/邮箱/微信"
        if AD_CTA_RE.search(head[max(0, m.start() - 100):m.start() + 80]):
            return True
    return False


def is_low_value(article: dict, rules: dict) -> bool:
    """标题或正文开头命中低信息密度关键词(公告/日历/预警/内部活动等)"""
    title = article.get("title", "")
    head = article.get("text", "")[:300]
    extra = rules.get("internal_activity", [])
    return any(k in title or k in head for k in LOW_VALUE_KEYWORDS + extra)


def interest_hit(kw: str, text_lower: str):
    """兴趣匹配: 纯英文/数字关键词要求词边界, 避免 'ai' 误匹配 'said' 等"""
    if re.fullmatch(r"[a-z0-9]+", kw.lower()):
        return re.search(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])", text_lower)
    return kw.lower() in text_lower


def marketing_penalty(title: str, text: str, rules: dict, source: str = "") -> tuple:
    """返回 (score_delta, is_marketing, reason)。
    否决级: 标题强营销词 / 广告自认(含裸「广告」标记) / 来源自我宣传 / 图书带货 /
            报告宣发 / 企业活动宣传稿(邀约+活动组合) / 企业PR栏目 / B2B软文 / 官方套话≥3
    扣分级: 标题弱营销词 / 正文营销词 / 群引流 / 官方套话少量 / 广告区域"""
    head = text[:1500]
    strong_title = [w for w in STRONG_MARKETING if w in title]
    strong_head = [w for w in STRONG_MARKETING if w in head]
    weak_title = [w for w in WEAK_MARKETING if w in title]

    if strong_title or len(weak_title) >= 2:
        words = (strong_title or weak_title)[:2]
        return -30, True, f"标题含营销词({', '.join(words)})"
    # 来源自我宣传: 公众号名在正文中高频出现且伴随宣传语境(关注引导/自我褒扬) → 一票否决
    # (如 "扫描下方二维码关注'赛迪顾问'公众号" / "结合赛迪顾问的深厚研究积淀";
    #  而 "今天丁香医生就来给大家讲讲" 这类编辑口吻的自引不算宣传, 避免误杀)
    src = (source or "").strip()
    if src and len(src) >= 4:
        promo_ctx = re.compile(r"关注|二维码|公众号|微信号|深厚|积淀|优势|领先|实力|权威")
        n_promo = 0
        for m in re.finditer(re.escape(src), text):
            ctx = text[max(0, m.start() - 40):m.end() + 40]
            if promo_ctx.search(ctx):
                n_promo += 1
        # 8/25 修复: 阈值读配置(默认3), 不再硬编码2——原硬编码导致
        # 腾讯研究院AI周报(自引2次: 标题+自家ima知识库二维码)被误杀, 其主体是数据干货
        if n_promo >= rules.get("source_self_promo_min", 3):
            return -40, True, f"来源自我宣传({src}宣传式自引{n_promo}次)"
    # 广告自认: 强声明(本文为广告/供应方提供/本文为推广信息等)全文扫描;
    # 弱括号标记(（广告）/【广告】等)仅扫前 2000 字, 且排除媒体文末广告位模板
    # ("点击图片 即刻购买（广告）"——南风窗等媒体每篇都带的通用变现位, 非文章广告)
    # → 一票否决
    # 8/25 修复: 强声明改全文, 拦住文末披露"（本文为推广信息）"的软文
    # (南方周末《刘慈欣推介！这门写作课》, 自认标记在 4294 位置);
    # 弱标记加"即刻购买/点击图片"等模板排除, 避免南风窗短文(正文仅1133字,
    # 广告位落在1021位置)整站误杀
    self_ad = [w for w in rules.get("self_ad", []) if w in text]
    self_ad_weak = []
    for w in rules.get("self_ad_weak", []):
        for m in re.finditer(re.escape(w), text[:2000]):
            if re.search(r"即刻购买|点击图片|长按识别|扫一扫|扫码查看", text[max(0, m.start() - 25):m.start()]):
                continue  # 媒体文末广告位模板, 非文章自认广告
            self_ad_weak.append(w)
            break
    if self_ad or self_ad_weak:
        return -40, True, f"正文自认广告({(self_ad or self_ad_weak)[0]})"
    # 图书/课程带货: 图书词 + 购买CTA 同现 → 一票否决
    # (正常书评提"出版社"但无购买CTA, 组合检测避免误杀)
    bp_words = [w for w in rules.get("book_promo_words", []) if w in title or w in text[:2000]]
    bp_cta = [w for w in rules.get("book_promo_cta", []) if w in text[:2000]]
    if bp_words and bp_cta:
        return -40, True, f"图书带货软文({bp_words[0]}+{bp_cta[0]})"
    # 课程/训练营带货: 课程词 + (报名CTA 或 具体价格) 同现 → 一票否决
    # 8/25 新增: 漏网案例 南方周末《刘慈欣推介！这门写作课》,
    # 结构为"干货开头(科幻科普+作家访谈) + 中后段硬广(优惠价仅需199元/269元 + 点击报名按钮)",
    # 名人背书(刘慈欣推介)只是幌子, 核心是卖写作课。词表刻意避开"课程""培训"等宽泛词防误杀
    # (如"AI课程报道""研究生培养"等正常语境), 只取营销色彩浓的课程词。
    cp_words = [w for w in rules.get("course_promo_words", []) if w in title or w in text]
    cp_cta = [w for w in rules.get("course_promo_cta", []) if w in text]
    cp_price = bool(re.search(rules.get("course_price_re", r"\d{2,4}\s*元"), text))
    if cp_words and (cp_cta or cp_price):
        return -40, True, f"课程带货软文({cp_words[0]}+{'价格' if cp_price else cp_cta[0]})"
    # 自营产品带货软文(8/25第2轮新增): 自营产品信号 + 促销词 + 具体价格 同现 → 一票否决
    # 案例: 丁香医生《别不信！饭前偷喝这一杯，真能帮你少吃点！》——800字科普引子
    # + 3200字自家产品(吉零膳食纤维粉)硬广: "丁香自家研发" + "开团价直降10元/只要137元/
    # 单条到手低至1.53元" + "戳图买3盒更划算/赶紧冲/囤起来" + 文末"商品信息/活动时间"。
    # 判断原则(8/25用户校准): 内容质量与广告位是两个维度——文末作者简介、公众号导流、
    # 小程序推荐等固定广告位不影响高质量文章入选; 只有"文章主体就是卖货"才否决。
    # 故 author_promo/app_promo(文末信号)已删除, 此处只拦"全文大比例带货"的自营促销文。
    ss_words = [w for w in rules.get("shop_self_words", []) if w in text]
    ss_promo = [w for w in rules.get("shop_promo_words", []) if w in text]
    ss_price = bool(re.search(r"\d{2,4}\s*元", text))
    if ss_words and len(ss_promo) >= 2 and ss_price:
        return -40, True, f"自营产品带货软文({ss_words[0]}+促销x{len(ss_promo)}+价格)"
    # 报告宣发: 标题即报告名(年报/白皮书等), 或 正文"发布《...报告》" + 卖报告 CTA
    # 8/25 修复: "半年报"含"年报"子串, 财报新闻(如"江淮发布2026年上半年报")被误杀 → 排除
    rt = [w for w in rules.get("report_title", []) if w in title and not (w == "年报" and "半年报" in title)]
    rm = [w for w in rules.get("report_mid", []) if w in title]
    rc = [w for w in rules.get("report_cta", []) if w in text[:800]]
    if rt:
        return -40, True, f"标题为报告宣发({rt[0]})"
    if rm and (rc or re.search(r"发布《[^》]*报告", text[:400])):
        return -40, True, "研究报告宣发(标题+卖报告CTA)"
    if re.search(r"发布《[^》]*报告", text[:400]):
        return -40, True, "正文为研究报告发布"
    # 企业/机构活动宣传稿: 邀约词 + 活动词 同现(标题或正文开头) → 一票否决
    # (如 "受邀为XX作专题讲座" / "应邀出席培训"; 正常新闻罕用此句式)
    inv = [w for w in rules.get("corp_invite", []) if w in title or w in head[:400]]
    evt = [w for w in rules.get("corp_event", []) if w in title or w in head[:400]]
    if inv and evt:
        return -40, True, f"企业活动宣传稿({inv[0]}+{evt[0]})"
    # 企业PR栏目名: 标题命中即否决("顾问之声｜""喜讯｜"等)
    pr_pat = [w for w in rules.get("pr_title_pattern", []) if w in title]
    if pr_pat:
        return -40, True, f"企业PR栏目({pr_pat[0]})"
    # 新车上市PR稿(8/25新增): 标题"XX正式上市/新车上市/全球首发" + 感叹号宣传文案 → 一票否决
    # (车云《不造『速成车』，品质不双标！ID. ERA 5S正式上市》98分漏网案例,
    #  全文歌功颂德无具体售价; 正常上市新闻如"小米YU7正式上市，售价21.59万起"不带感叹号, 不误杀)
    car_launch = [w for w in rules.get("car_launch_title", []) if w in title]
    if car_launch and "！" in title:
        return -40, True, f"新车上市PR稿({car_launch[0]}+感叹号宣传)"
    # B2B 软文: 标题命中软文结构 + 正文有访谈/邀约特征 → 否决; 仅标题 → 重扣
    b2b_title = [w for w in rules.get("b2b_soft_title", []) if w in title]
    b2b_interview = [w for w in rules.get("b2b_interview", []) if w in text[:400]]
    if b2b_title and b2b_interview:
        return -40, True, f"B2B软文(标题{b2b_title[0]}+访谈)"
    delta, reasons = 0, []
    if b2b_title:
        delta -= 20
        reasons.append(f"标题疑似企业软文({b2b_title[0]})")
    # 官方套话/政务文体: 少量命中扣分, ≥3 视为官方宣传稿否决
    off_hits = [w for w in rules.get("official_register", []) if w in text[:1500]]
    if len(off_hits) >= 3:
        return -40, True, f"官方宣传文体(套话x{len(off_hits)})"
    elif off_hits:
        delta -= min(len(off_hits) * 5, 10)
        reasons.append(f"官方文体词({off_hits[0]})")
    # 群引流: 开头 200 字加微信/入群 → 扣分(内容好可保留)
    drain = [w for w in rules.get("group_drain", []) if w in text[:200]]
    if drain:
        delta -= 12
        reasons.append(f"开头群引流({drain[0]})")
    if weak_title:
        delta -= 8
        reasons.append(f"标题提及弱营销词({weak_title[0]})")
    if strong_head:
        delta -= min(len(strong_head) * 4, 12)
        reasons.append(f"正文提及营销词({', '.join(strong_head[:2])})")
    if is_ad_zone(text):
        return delta - 30, True, "正文夹带硬广区域"
    return delta, False, "; ".join(reasons)

SKIP_KEYWORDS = ["在小说阅读器", "去阅读", "沉浸阅读", "Original",
                 "The following article is From", "微信扫一扫关注该公众号",
                 "预览时标签不可点", "收录于合集", "个相关内容"]

# ---------------------------------------------------------------- 质量规则(可经 config.json quality_rules 覆盖调参)
# 背景: 8/21 用户反馈日报混入"长正文营销文"(报告宣发/企业软文)与"目录撑篇幅"短文,
#       以及微信抓取界面噪声虚增字数。新增: 广告自认/报告宣发/B2B软文=一票否决, 群引流=扣分,
#       内部活动/党建/卖书=低值剔除, 有效字数替代原始字数判定。
DEFAULT_QUALITY_RULES = {
    # 广告自认·强声明: 全文出现即一票否决(如 "*本文为广告，内容由供应方提供"、
    # 8/25 新增 "本文为推广信息"/"本文为推广" 拦截文末免责声明式软文)
    "self_ad": ["本文为广告", "内容由供应方提供", "特约发布", "品牌方提供",
                "本文为推广信息", "本文为推广"],
    # 广告自认·弱括号标记: 仅前 2000 字命中才否决(FT中文网软广开头的独立「广告」标记;
    # 不做全文扫描, 防南风窗等媒体文末通用广告位模板"点击图片 即刻购买（广告）"误杀)
    # 8/25 新增 "- 广告 -" 与 "-广告-"(丁香医生带货文中独立广告标记的带/不带空格变体)
    "self_ad_weak": ["推广内容", "广告内容", "「广告」", "（广告）", "【广告】", "[广告]", "(广告)",
                     "- 广告 -", "-广告-"],
    # 图书/课程带货: 图书词 + 购买CTA 同现 → 一票否决(如 BBC科普三部曲卖书软广)
    # 注意: 正常书评会提到"出版社"但不会带购买CTA, 组合检测避免误杀
    "book_promo_words": ["这套书", "三部曲", "当当网", "京东图书", "限量发售", "新书首发", "套装",
                          "折上折", "秒杀价", "豆瓣阅读", "听书卡"],
    "book_promo_cta": ["点击购买", "立即购买", "扫码购买", "购买链接", "特惠", "限量", "抢购",
                        "扫描下方二维码", "戳我购买", "现在下单"],
    # 课程/训练营带货: 课程词 + (报名CTA 或 价格) 同现 → 一票否决(8/25 新增)
    # 案例: 南方周末《刘慈欣推介！这门写作课》"优惠价仅需199元/269元 + 点击报名按钮"
    "course_promo_words": ["写作课", "训练营", "音频课", "精品课", "直播课", "购课", "学习资料包",
                           "答疑视频", "讲师答疑", "报名按钮", "课程福利"],
    "course_promo_cta": ["优惠价", "仅需", "点击报名", "报名按钮", "扫码报名", "加入学习",
                         "立即加入", "店铺了解", "戳我", "扫码购", "立即抢课"],
    "course_price_re": r"\d{2,4}\s*元",
    # 自营产品带货软文(8/25第2轮新增): 自营产品信号 + 促销词≥2 + 具体价格 → 一票否决
    # 案例: 丁香医生《别不信！饭前偷喝这一杯》"丁香自家研发"+"开团价直降10元/只要137元/
    # 单条到手低至1.53元"+"戳图买3盒更划算/赶紧冲/囤起来"。词表刻意避开"产品""推荐"等
    # 宽泛词, 只取营销色彩浓的自营/促销信号, 防误杀正常健康科普或测评文。
    "shop_self_words": ["自家研发", "自家出品", "自研", "自有品牌", "自家店", "限时开团", "开团"],
    "shop_promo_words": ["开团价", "直降", "立省", "领券", "加赠", "买 3 盒", "买3盒", "单盒到手",
                         "单条到手", "到手价", "戳图", "赶紧冲", "囤起来", "限时优惠", "特惠价",
                         "秒杀", "前 500 名", "前500名"],
    # 企业/机构活动宣传稿: 邀约词 + 活动词 同现(标题或正文开头) → 一票否决
    # 如 "顾问之声｜XX总经理受邀为XX公司作专题讲座"(8/24 漏网案例)
    "corp_invite": ["受邀", "应邀"],
    "corp_event": ["专题讲座", "讲座", "培训班", "培训", "论坛", "峰会", "党校", "党性教育",
                    "签约仪式", "开班仪式", "考察调研", "莅临指导", "表彰大会"],
    # 企业PR栏目名: 标题命中即否决
    "pr_title_pattern": ["顾问之声", "公司动态", "战略签约", "应邀出席", "受邀出席",
                          "公司新闻", "要闻速递", "喜讯", "捷报"],
    # 新车上市PR稿: 标题"XX正式上市/新车上市/全球首发" + 感叹号宣传语 → 一票否决(8/25 新增)
    # 案例: 车云《不造『速成车』，品质不双标！ID. ERA 5S正式上市》
    "car_launch_title": ["正式上市", "新车上市", "上市发布会", "全球首发"],
    # 官方套话/政务文体: 命中≥3 视为官方宣传稿否决, 少量命中则扣分
    "official_register": ["深入学习贯彻", "重要讲话精神", "思想政治建设", "充分肯定", "圆满成功",
                          "圆满举办", "取得良好反响", "高度重视", "参训学员", "亲切交谈",
                          "表示热烈祝贺", "一致好评"],
    # 来源自我宣传: 公众号名在正文中出现≥N次(自己发自己宣传) → 一票否决
    "source_self_promo_min": 3,
    # 报告宣发: 标题含这些词即视为报告名/报告推广(正常文章标题不会用)
    "report_title": ["年报", "白皮书", "蓝皮书", "年度报告"],
    # 报告宣发(需配合): 标题含"市场研究/研究报告" + 正文卖报告 CTA 或"发布《..报告》"
    "report_mid": ["市场研究", "研究报告"],
    "report_cta": ["后台联系我们", "完整报告", "报告全文", "获取报告", "购买报告", "详情可后台", "订阅报告", "索取报告"],
    # 群引流/私域导流: 正文前 200 字命中 → 扣分(内容好可保留)
    "group_drain": ["加微信", "入群", "出示名片", "扫码进群", "添加群主", "进群",
                    "扫描下方二维码关注", "微信号："],
    # B2B 软文标题特征: 标题命中 + 正文访谈/邀约 → 一票否决; 仅标题命中 → 重扣
    "b2b_soft_title": ["解决方案", "量产破局", "破局之路", "全链响应", "创新引领", "交流大会", "圆满落幕", "量产密码"],
    "b2b_interview": ["本期访谈", "特邀", "做客", "专访"],
    # 内部活动/党建/宣传报道(低值): 标题命中即剔除
    "internal_activity": ["圆满落幕", "观影", "党建", "党性教育", "党校", "培训班", "工会",
                          "交流会", "学习强国", "金句", "现场活动", "团建", "启动仪式", "参训"],
    # 小程序弹窗文(无有效正文): 前 500 字命中即整篇剔除
    "mini_program": ["Scan with Weixin to", "微信扫一扫可打开此内容", "使用完整服务", "Got It"],
}

# 微信抓取噪声锚点: 头部(标题/作者/关注引导)与尾部(互动按钮/服务菜单/账号关注引导)
HEAD_NOISE_ANCHORS = ["在小说阅读器读本章", "点击蓝字，关注我们", "点击上方", "微信扫一扫关注该公众号"]
TAIL_NOISE_ANCHORS = ["服务  :", "轻点两下取消赞", "Video", "Mini Program", "Share", "Comment",
                      "Favorite", "听过", "Scan to Follow", "一键关注", "微信矩阵", "点亮星标",
                      "关注公众号", "关注我们", "Scan with Weixin"]


def clean_text(raw: str, rules: dict) -> str:
    """清洗微信抓取噪声, 返回有效正文(用于字数判定/评分/摘要)。
    1) 小程序弹窗文(正文被授权弹窗占满) → 返回空串
    2) 头部: 取最靠后的关注引导锚点之后作为正文起点
    3) 尾部: 取最早出现的互动按钮锚点之前作为正文终点"""
    if not raw:
        return ""
    for k in rules.get("mini_program", []):
        if k in raw[:500]:
            return ""
    start, end = 0, len(raw)
    cands = [(raw.find(a) + len(a), a) for a in HEAD_NOISE_ANCHORS]
    head_starts = [(p, a) for p, a in cands if 0 <= p - len(a) < 800]
    if head_starts:
        start = max(p for p, _ in head_starts)
    tail_pos = [raw.find(a) for a in TAIL_NOISE_ANCHORS]
    tail_pos = [p for p in tail_pos if p > 0 and len(raw) - p < 600]
    if tail_pos:
        end = min(tail_pos)
    clean = raw[start:end]
    for w in ["Original", "在小说阅读器中沉浸阅读", "在小说阅读器读本章", "去阅读",
              "收录于合集", "预览时标签不可点", "个相关内容", "The following article is From"]:
        clean = clean.replace(w, " ")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def topic_hits(article: dict, cfg: dict) -> dict:
    """识别文章命中的高权重主题(如人工智能/新能源汽车)。
    命中门槛: 标题命中 1 个关键词, 或正文前 3000 字命中 ≥2 个不同关键词
    (避免正文偶提一次的 'AI'/'蔚来' 等造成误标)。
    返回 {主题名: {hits:[命中的关键词], bonus, quota, min_chars}}"""
    title_lower = article.get("title", "").lower()
    text_lower = article.get("text", "")[:3000].lower()
    result = {}
    for topic, spec in cfg.get("topic_weights", {}).items():
        kws = spec.get("keywords", [])
        title_hits = [kw for kw in kws if interest_hit(kw, title_lower)]
        text_hits = [kw for kw in kws
                     if kw not in title_hits and interest_hit(kw, text_lower)]
        if title_hits or len(text_hits) >= 2:
            result[topic] = {"hits": (title_hits + text_hits)[:3], **spec}
    return result


def count_tags(content_html: str, tag: str) -> int:
    return len(re.findall(rf"<{tag}[\s>]", content_html, flags=re.I))


def heuristic_score(article: dict, cfg: dict) -> dict:
    title = article["title"]
    text = article.get("clean_text") or article["text"]
    content_html = article["content_html"]
    rules = {**DEFAULT_QUALITY_RULES, **cfg.get("quality_rules", {})}
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

    # 图多文少: 图集/海报式内容(如报告截图堆砌) → 扣分
    imgs = count_tags(content_html, "img")
    if imgs >= 20 and chars < 2500:
        score -= 10
        reasons.append(f"图片 {imgs} 张而正文仅 {chars} 字, 疑似图集/海报")

    interests = [kw.lower() for kw in cfg.get("interests", []) if kw]
    if interests:
        text_lower = (title + " " + text[:3000]).lower()
        hits = [kw for kw in interests if interest_hit(kw, text_lower)]
        if hits:
            score += min(len(hits) * 4, 12)
            reasons.append(f"命中兴趣: {'、'.join(hits[:3])}")

    # 高权重主题加分(topic_weights: 人工智能/新能源汽车为最高权重)
    topics = topic_hits(article, cfg)
    if topics:
        for tname, tspec in topics.items():
            score += tspec.get("bonus", 0)
            reasons.append(f"高权重主题「{tname}」+{tspec.get('bonus', 0)}"
                           f"(命中: {'、'.join(tspec['hits'])})")

    m_delta, m_flag, m_reason = marketing_penalty(title, text, rules, article.get("source", ""))
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
    title = article["title"]
    text = article.get("clean_text") or article["text"]
    content_html = article["content_html"]
    rules = {**DEFAULT_QUALITY_RULES, **cfg.get("quality_rules", {})}
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

    imgs = count_tags(content_html, "img")
    if imgs >= 20 and chars < 2500:
        score -= 10

    # 探索区仅反向规避高权重主题(主线已保 AI/新能源), 不再对兴趣扣分——质量优先, 多样性其次
    topics = topic_hits(article, cfg)
    if topics:
        score -= sum(t.get("bonus", 0) for t in topics.values())
        reasons.append(f"探索区避开高权重主题({'、'.join(topics)})")

    m_delta, m_flag, m_reason = marketing_penalty(title, text, rules, article.get("source", ""))
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
        digest = clean_digest(a.get("clean_text") or a["text"], a["title"])
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

    quality_rules = {**DEFAULT_QUALITY_RULES, **cfg.get("quality_rules", {})}
    # 第0层 · 来源信誉: 黑名单公众号直接剔除(纯企业宣传号/营销号, 借鉴反垃圾 sender reputation)
    source_blacklist = set(cfg.get("source_blacklist", []))
    lookback = now - timedelta(days=cfg.get("lookback_days", 3) - 1)
    lookback = lookback.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates, stale_note, skipped_low, skipped_black = [], [], 0, 0
    for a in all_articles:
        try:
            pub = datetime.strptime(a["pub_date"][:10], "%Y-%m-%d")
        except Exception:
            continue
        if pub.date() < lookback.date():
            continue
        if a.get("source") in source_blacklist:
            skipped_black += 1
            continue
        # 兼容三种历史键: 完整链接 / id:slug / title:标题 (与本地 digest.py 互通)
        slug = a["link"].split("/s/")[-1].split("?")[0].split("#")[0]
        if a["link"] in sent or ("id:" + slug) in sent or ("title:" + a["title"].strip()) in sent:
            continue
        a["clean_text"] = clean_text(a.get("text", ""), quality_rules)  # 清洗微信噪声, 后续字数/评分/摘要均用它
        if is_low_value(a, quality_rules):   # 低信息密度内容(公告/日历/预警/内部活动等)直接剔除
            skipped_low += 1
            continue
        candidates.append(a)
    print(f"[+] 回溯 {cfg.get('lookback_days', 3)} 天且未推送过: {len(candidates)} 篇"
          f" (另有 {skipped_low} 篇低信息密度 + {skipped_black} 篇黑名单来源已剔除)")

    # 无新文章: 发提示邮件(不中断, 保持系统可感知)
    if not candidates:
        body = (f"<p>今天没有新文章可推荐。</p>"
                f"<p>本地数据最后更新时间: {exported_at}(北京时间)。</p>"
                f"<p>若已超过一天, 说明抓取电脑最近没有开机, 开机后会自动恢复。</p>")
        send_mail(f"公众号日报 · {run_date} · 今日暂无新文章", body, cfg)
        print("[+] 已发送'暂无新文章'通知")
        return

    # 评分 + 主题识别 + 精选(主线 + 自由探索, 与本地版同一套逻辑)
    for a in candidates:
        a["eval"] = heuristic_score(a, cfg)
        a["topics"] = topic_hits(a, cfg)

    limit = cfg.get("max_results", 8)
    explore_count = min(cfg.get("explore_count", 2), limit // 2)
    main_count = limit - explore_count
    rng = random.Random(run_date)

    ranked = sorted(candidates, key=lambda a: a["eval"]["score"], reverse=True)
    default_min = cfg.get("min_chars", 1500)

    def qualified(a, min_chars):
        return (not a["eval"]["flags"].get("marketing")
                and len(a.get("clean_text") or a["text"]) >= min_chars)

    # 主线: 先按高权重主题配额直选(AI/新能源有货必保, 宁缺毋滥不强凑), 剩余名额按分数竞争
    # 来源去重: 同一公众号最多选1篇, 多篇好文留改天推荐(用户8/22反馈4篇同源问题)
    main_selected, selected_links, picked_sources = [], set(), set()
    for topic, spec in cfg.get("topic_weights", {}).items():
        qmin = spec.get("min_chars") or default_min
        n_topic = len([a for a in candidates if topic in a.get("topics", {})])
        pool = [a for a in candidates
                if topic in a.get("topics", {})
                and a["link"] not in selected_links      # 跨主题去重(已入选不再参与)
                and not (a.get("source") and a["source"] in picked_sources)  # 来源去重
                and qualified(a, qmin)]
        pool.sort(key=lambda a: a["eval"]["score"], reverse=True)
        take = min(spec.get("quota", 2), len(pool))
        if take == 0:
            print(f"[+] {topic}: 候选 {n_topic} 篇, 达标 0 篇, 名额让给其他方向(宁缺毋滥)")
            continue
        print(f"[+] {topic}: 候选 {n_topic} 篇, 达标 {len(pool)} 篇, 配额直选 {take} 篇")
        taken = 0
        for a in pool:
            if taken >= take:
                break
            if a.get("source") and a["source"] in picked_sources:
                continue  # 来源已选(跨主题去重)
            a["type"] = "main"
            main_selected.append(a)
            selected_links.add(a["link"])
            if a.get("source"):
                picked_sources.add(a["source"])
            taken += 1

    remaining = main_count - len(main_selected)
    if remaining > 0:
        # 第一轮: 来源不重复(同一公众号最多1篇)
        rest_pool = [a for a in ranked
                     if a["link"] not in selected_links
                     and qualified(a, default_min)]
        print(f"[+] 其余方向按分数竞争剩余 {remaining} 个名额(候选 {len(rest_pool)} 篇, 来源不重复)")
        for a in rest_pool:
            if len(main_selected) >= main_count:
                break
            if a.get("source") and a["source"] in picked_sources:
                continue  # 同来源已选, 留改天推荐
            a["type"] = "main"
            main_selected.append(a)
            selected_links.add(a["link"])
            if a.get("source"):
                picked_sources.add(a["source"])
        # 不在主线放宽来源限制——主线不足的名额全部转入探索区
        # 探索区门槛更低(min_explore_chars), 能纳入更多不同来源的好文
    if len(main_selected) < main_count:
        deficit = main_count - len(main_selected)
        print(f"[+] 主线达标仅 {len(main_selected)} 篇(<{main_count}), "
              f"剩余 {deficit} 个名额转入自由探索(质量优先, 全部探索文亦可)")
        explore_count += deficit
    main_selected = main_selected[:main_count]

    selected = list(main_selected)
    if explore_count > 0:
        chosen_links = {a["link"] for a in selected}
        # picked_sources 已在主线阶段维护(含来源去重), 直接复用
        min_explore = cfg.get("explore_min_chars", 1200)
        pool = [a for a in candidates
                if a["link"] not in chosen_links
                and not a["eval"]["flags"].get("marketing")
                and len(a.get("clean_text") or a["text"]) >= min_explore]
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
        for a in explore_ranked:  # 第二轮: 放宽来源限制(同来源最多2篇, 避免过度集中)
            if len(selected) >= limit:
                break
            if a["link"] in chosen_links:
                continue
            src_count = sum(1 for s in selected if s.get("source") == a.get("source"))
            if src_count >= 2:
                continue  # 同来源已有2篇, 留改天推荐
            a["eval"] = a["explore_eval"]
            a["type"] = "explore"
            selected.append(a)
            chosen_links.add(a["link"])

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
        ttags = "".join(f"[{t}]" for t in a.get("topics", {}))
        print(f"  {i}. {tag}{ttags}[{a['eval']['score']:.0f}分] {a['title']} ({a['source']})")

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
