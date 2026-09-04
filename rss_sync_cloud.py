#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rss_sync_cloud.py — 微信公众号精选日报 (RSS 重源) 的云端抓取同步入口
==========================================================
作用: 在 GitHub Actions 临时 runner 上独立完成, 把 RSS 源的近期文章抓取、过滤软文、

  写入 data/rss_articles.json (独立于 WeChat 源 data/articles_recent.json, 互不干扰):
    ① 拉取 rss_sources.json 中所有已验证 RSS 源
    ② 软文/广告前置过滤 (config.json rss_soft_filter)
    ③ 解析为 digest_cloud.py 可消费的字段 (link 用真实文章 URL, 非假链接)
    ④ 与已有的 data/rss_articles.json 按标题去重合并 (保留近 N 天, 增量更新)
    ⑤ 用 GitHub Contents API 推回仓库

为什么独立成 rss_articles.json:
  - articles_recent.json 由本机 sync_data.py 维护 (WeChat 源, 文件较大 ~30MB),
    RSS 源独立维护可避免云端每 2 小时下载/合并 30MB 文件。
  - digest_cloud.py 会同时读取两者并合并, 实现"WeChat + RSS"双源精选。

与本机 fetch_rss.py 的关系:
  - 本机 fetch_rss.py 保留用于手动/排查 (写入本地 WeWe RSS DB)
  - 本脚本是云端唯一入口: 零外部依赖(无 WeWe RSS, 无本地路径)
"""
import json, os, re, html, hashlib, sys, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.request import Request, build_opener, ProxyHandler, urlopen
from xml.etree import ElementTree as ET

PROJ = Path(__file__).resolve().parent
SRC = PROJ / "rss_sources.json"
OUT_JSON = PROJ / "data" / "rss_articles.json"
DAYS_KEEP = int(os.environ.get("DAYS_KEEP", "10"))        # 本地保留窗口(防文件无限增长)
REPO_PATH = os.environ.get("REPO_PATH", "HZ1984/wechat-digest")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
opener = build_opener(ProxyHandler({}))


# ============================ GitHub Contents API ============================
def get_token() -> str:
    """token 优先级: env GH_TOKEN > Windows 凭据管理器(wincred) > sync_config.json。
    云端由 workflow 注入 GH_TOKEN(secrets.GITHUB_TOKEN), 本机则复用 git 已存的凭据(静默, 不弹窗)。"""
    tok = os.environ.get("GH_TOKEN", "").strip()
    if tok:
        return tok
    try:
        tok = _read_wincred("git:https://github.com").strip()
        if tok:
            return tok
    except Exception:
        pass
    cfg = PROJ / "sync_config.json"
    if cfg.exists():
        try:
            return (json.loads(cfg.read_text(encoding="utf-8")).get("token") or "").strip()
        except Exception:
            pass
    raise RuntimeError("找不到 GitHub token: 设置 env GH_TOKEN 或 sync_config.json.token")


def _read_wincred(target: str) -> str:
    """从 Windows 凭据管理器静默读取密码(advapi32 CredRead), 非 Windows/失败返回空。"""
    try:
        import ctypes
        adv = ctypes.windll.advapi32
    except Exception:
        return ""

    class CREDCRED(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.c_uint), ("Type", ctypes.c_uint),
            ("TargetName", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p),
            ("LastWritten", ctypes.c_ulong * 2), ("CredentialBlobSize", ctypes.c_uint),
            ("CredentialBlob", ctypes.c_void_p), ("Persist", ctypes.c_uint),
            ("AttribCount", ctypes.c_ulong), ("Attributes", ctypes.c_void_p),
            ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p),
        ]

    ptr = ctypes.c_void_p()
    if not adv.CredReadW(target, 1, 0, ctypes.byref(ptr)) or not ptr:
        return ""
    try:
        c = CREDCRED.from_address(ptr.value)
        blob = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
        return blob.decode("utf-16-le", errors="replace")
    finally:
        adv.CredFree(ptr)


def _api_get(path: str, token: str):
    url = f"https://api.github.com/repos/{REPO_PATH}/contents/{path}"
    req = Request(url, headers={"Authorization": f"token {token}",
                                 "Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def download_existing(token: str):
    """下载仓库已有的 data/rss_articles.json (经 download_url, 支持任意大小)。无则返回 []。"""
    try:
        meta = _api_get("data/rss_articles.json", token)
    except Exception:
        return []
    dl = meta.get("download_url")
    if not dl:
        return []
    try:
        with urlopen(Request(dl, headers={"Authorization": f"token {token}"}), timeout=60) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return data.get("articles", []) if isinstance(data, dict) else []
    except Exception:
        return []


def push_to_github(local_path: Path, message: str, token: str) -> bool:
    api = f"https://api.github.com/repos/{REPO_PATH}/contents/data/rss_articles.json"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    sha = None
    try:
        with urlopen(Request(api, headers=headers), timeout=30) as r:
            sha = json.loads(r.read()).get("sha")
    except Exception:
        pass
    import base64
    body = base64.b64encode(local_path.read_bytes()).decode("ascii")
    payload = {"message": message, "content": body, "branch": "main"}
    if sha:
        payload["sha"] = sha
    req = Request(api, data=json.dumps(payload).encode("utf-8"),
                  headers={**headers, "Content-Type": "application/json"}, method="PUT")
    try:
        with urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
            print(f"[+] 推送成功 commit={d.get('commit', {}).get('sha', '')[:7]}")
            return True
    except Exception as e:
        print(f"[error] 推送失败: {type(e).__name__}: {e}")
        try:
            print(getattr(e, "read", lambda: b"")().decode("utf-8", "replace")[:500])
        except Exception:
            pass
        return False


# ============================ 软文过滤 (从 config.json 加载) ============================
def _soft_filter(cfg_path: Path):
    try:
        soft = json.loads(cfg_path.read_text(encoding="utf-8")).get("rss_soft_filter", {})
    except Exception:
        soft = {}
    subj = soft.get("subjects", []) or []
    tpl = [t for t in soft.get("title_templates_re", []) if t]
    title_re = re.compile("|".join(f"(?:{t})" for t in tpl)) if tpl else None
    promo_res = [re.compile(w) for w in soft.get("promo_words", [])]
    return subj, title_re, promo_res


def is_soft_article(title, text, subj, title_re, promo_res):
    if not title:
        return False, ""
    if subj and title_re:
        s = [x for x in subj if x in title]
        if s and title_re.search(title):
            return True, f"消费品={s[0]} + 诱购"
    head = (text or "")[:2000]
    head = re.sub(r"<[^>]+>", "", head)
    head = re.sub(r"\s+", " ", head).strip()
    combined = title + "\n" + head
    for r in promo_res:
        m = r.search(combined)
        if m:
            return True, f"促销话术={m.group(0)}"
    return False, ""


# ============================ RSS 解析 ============================
def article_id(url):
    return f"RSS_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}"


def parse_date(s):
    if not s:
        return 0
    s = s.strip()
    from email.utils import parsedate_to_datetime
    try:
        return int(parsedate_to_datetime(s).timestamp())
    except Exception:
        pass
    try:
        s2 = s.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s2).timestamp())
    except Exception:
        return 0


def strip_html(s):
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_rss(url):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, */*"})
    with opener.open(req, timeout=15) as r:
        raw = r.read().decode("utf-8", "replace")
    root = ET.fromstring(raw)
    items = []
    for item in root.iter("item"):
        title = strip_html(item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate") or ""
        desc = item.findtext("description") or ""
        pic = ""
        for ch in item:
            if ch.tag.endswith("content") and ch.attrib.get("url"):
                pic = ch.attrib["url"]; break
            if ch.tag.endswith("thumbnail") or ch.tag.endswith("image"):
                pic = (ch.text or "").strip(); break
            if "media" in ch.tag.lower() and "url" in ch.attrib:
                pic = ch.attrib["url"]; break
        if not pic:
            m = re.search(r'<img[^>]+src="([^"]+)"', desc)
            if m:
                pic = m.group(1)
        items.append({"title": title, "link": link,
                      "pub": parse_date(pub), "summary": strip_html(desc)[:1500], "pic": pic})
    if not items:  # Atom
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = strip_html(entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            le = entry.find("{http://www.w3.org/2005/Atom}link")
            link = le.attrib.get("href", "") if le is not None else ""
            pub = (entry.findtext("{http://www.w3.org/2005/Atom}published") or
                   entry.findtext("{http://www.w3.org/2005/Atom}updated") or "")
            summary = (entry.findtext("{http://www.w3.org/2005/Atom}summary") or
                       entry.findtext("{http://www.w3.org/2005/Atom}content") or "")
            items.append({"title": title, "link": link,
                          "pub": parse_date(pub), "summary": strip_html(summary)[:1500], "pic": ""})
    return items


def norm_title(t):
    return re.sub(r"\s+", "", (t or "").strip().lower())


# ============================ 主流程 ============================
def main():
    sources = json.loads(SRC.read_text(encoding="utf-8"))["sources"]
    verified = [(mp, info) for mp, info in sources.items() if info.get("rss")]
    print(f"[rss-sync] 启用 RSS 源: {len(verified)}")

    subj, title_re, promo_res = _soft_filter(PROJ / "config.json")
    print(f"[rss-sync] 软文过滤: subjects={len(subj)} title_tpl={bool(title_re)} promo={len(promo_res)}")

    # 拉取并构建 RSS 文章 (正确格式, 真实链接)
    fresh = {}
    total_new = total_skip = total_soft = 0
    for mp_id, info in verified:
        url = info["rss"]
        name = info["name"]
        try:
            items = fetch_rss(url)
        except Exception as e:
            print(f"  [{name}] 拉取失败: {type(e).__name__}: {str(e)[:80]}")
            continue
        new = skip = soft = 0
        for it in items:
            if not it["link"] or not it["title"]:
                skip += 1; continue
            s, reason = is_soft_article(it["title"], it["summary"], subj, title_re, promo_res)
            if s:
                soft += 1; continue
            aid = article_id(it["link"])
            pub = it["pub"] or int(time.time())
            pub_dt = datetime.fromtimestamp(pub)
            summary = it["summary"] or ""
            art = {
                "id": aid,
                "title": it["title"][:255],
                "link": it["link"],
                "pub_date": pub_dt.strftime("%Y-%m-%d %H:%M"),
                "content_html": f"<p>{html.escape(summary)}</p>" if summary else "",
                "text": summary,
                "source": name,
                "publish_time": str(pub),
            }
            key = norm_title(it["title"])
            if key in fresh:
                skip += 1; continue
            fresh[key] = art
            new += 1
        total_new += new; total_skip += skip; total_soft += soft
        print(f"  [{name}] 新增 {new} / 跳过 {skip} / 软文 {soft}")
    print(f"[rss-sync] 本轮抓取: 新增 {total_new} / 软文拦截 {total_soft}")

    # 与已有 rss_articles.json 合并 (保留历史, 增量更新)
    existing = download_existing(get_token())
    print(f"[rss-sync] 云端已有 RSS 文章: {len(existing)}")
    merged = {}
    for a in existing:
        merged[norm_title(a.get("title", ""))] = a
    # 本轮新鲜文章覆盖同名 (真实链接优先)
    n_override = 0
    for key, art in fresh.items():
        if key in merged:
            n_override += 1
        merged[key] = art
    print(f"[rss-sync] 覆盖更新 {n_override} 篇, 合并后 {len(merged)} 篇")

    # 修剪: 仅保留近 DAYS_KEEP 天
    cutoff_ts = int((datetime.now(timezone(timedelta(hours=8))) -
                     timedelta(days=DAYS_KEEP)).timestamp())
    kept = [a for a in merged.values()
            if (int(a.get("publish_time", "0") or 0) or 0) >= cutoff_ts]
    print(f"[rss-sync] 修剪至近 {DAYS_KEEP} 天: 保留 {len(kept)} 篇")

    out = {
        "exported_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "days": DAYS_KEEP,
        "n_sources": len({a.get("source") for a in kept if a.get("source")}),
        "source": "rss",
        "articles": kept,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"[rss-sync] 写入 {OUT_JSON} ({OUT_JSON.stat().st_size / 1024:.1f} KB)")

    # 推送
    token = get_token()
    msg = f"data: rss-sync {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')} ({len(kept)}篇)"
    if not push_to_github(OUT_JSON, msg, token):
        sys.exit(1)
    print(f"[rss-sync] DONE - {len(kept)} 篇 RSS 已推送至 {REPO_PATH}")


if __name__ == "__main__":
    main()
