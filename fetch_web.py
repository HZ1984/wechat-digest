#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_web.py — 官网直抓（本地运行，补回 61 源里"媒体机构型"的覆盖）
==============================================================
与云端 RSS 池(rss_articles.json)解耦，独立产出 data/web_articles.json：
  ① 读 sources_web.json（每源: 列表页 + 文章链接正则 + 标题字数 + 可选日期正则）
  ② 抓列表页 → 用 url_pattern 强过滤导航/页脚噪声 → 提取 (标题, 链接)
  ③ 抓文章页 → 通用 <p> 抽取正文（用于日报"精读摘要"）
  ④ 软文/广告前置过滤（复用 fetch_rss.is_soft_article, 来自 config.json rss_soft_filter）
  ⑤ 与已有 web_articles.json 按标题去重合并（保留近 N 天, 增量更新）
  ⑥ 用 GitHub Contents API 推回仓库（digest_cloud.py 会作为第 3 个源池合并）

运行: python fetch_web.py            # 正常抓取+正文
      SKIP_CONTENT=1 python fetch_web.py   # 仅列表(快速验证, 不抓正文)
"""
import json, os, re, html, hashlib, sys, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.request import Request, build_opener, ProxyHandler, urlopen
from urllib.parse import urljoin
from html.parser import HTMLParser

PROJ = Path(__file__).resolve().parent
SRC = PROJ / "sources_web.json"
OUT_JSON = PROJ / "data" / "web_articles.json"
DAYS_KEEP = int(os.environ.get("DAYS_KEEP", "10"))
REPO_PATH = os.environ.get("REPO_PATH", "HZ1984/wechat-digest")
SKIP_CONTENT = os.environ.get("SKIP_CONTENT", "") == "1"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
opener = build_opener(ProxyHandler({}))

# 复用 RSS 软文过滤（与 fetch_rss 同一套规则, 保证口径一致）
try:
    from fetch_rss import is_soft_article
except Exception:
    def is_soft_article(title, text):
        return False, ""


# ============================ GitHub Contents API（与 rss_sync_cloud 同逻辑） ============================
def get_token() -> str:
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
    raise RuntimeError("找不到 GitHub token")


def _read_wincred(target: str) -> str:
    try:
        import ctypes
        adv = ctypes.windll.advapi32
    except Exception:
        return ""
    class CREDCRED(ctypes.Structure):
        _fields_ = [("Flags", ctypes.c_uint), ("Type", ctypes.c_uint),
                    ("TargetName", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p),
                    ("LastWritten", ctypes.c_ulong * 2), ("CredentialBlobSize", ctypes.c_uint),
                    ("CredentialBlob", ctypes.c_void_p), ("Persist", ctypes.c_uint),
                    ("AttribCount", ctypes.c_ulong), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p)]
    ptr = ctypes.c_void_p()
    if not adv.CredReadW(target, 1, 0, ctypes.byref(ptr)) or not ptr:
        return ""
    try:
        c = CREDCRED.from_address(ptr.value)
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le", errors="replace")
    finally:
        adv.CredFree(ptr)


def _api_get(path: str, token: str):
    url = f"https://api.github.com/repos/{REPO_PATH}/contents/{path}"
    req = Request(url, headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def download_existing(token: str):
    try:
        meta = _api_get("data/web_articles.json", token)
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
    api = f"https://api.github.com/repos/{REPO_PATH}/contents/data/web_articles.json"
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


# ============================ HTML 工具 ============================
def detect_encoding(raw: bytes) -> str:
    m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:2000], re.I)
    if m:
        return m.group(1).decode().lower()
    return "utf-8"


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


class LinkExtractor(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.links = []
        self._cur_text = []
        self._in_a = False
        self._cur_href = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._in_a = True
            self._cur_href = dict(attrs).get("href")
            self._cur_text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            txt = html.unescape("".join(self._cur_text)).strip()
            if self._cur_href:
                self.links.append((txt, urljoin(self.base, self._cur_href)))
            self._in_a = False
            self._cur_href = None

    def handle_data(self, data):
        if self._in_a:
            self._cur_text.append(data)


def fetch_html(url: str, timeout: int = 15) -> str:
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/html, */*"})
    with opener.open(req, timeout=timeout) as r:
        raw = r.read()
    enc = r.headers.get_content_charset() or detect_encoding(raw)
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("gbk", "replace")


def extract_content(html_text: str) -> str:
    """通用正文抽取: 优先 <p> 段落; 不足则退化为去标签后取正文窗口。"""
    t = re.sub(r"<script[\s\S]*?</script>", " ", html_text, flags=re.I)
    t = re.sub(r"<style[\s\S]*?</style>", " ", t, flags=re.I)
    ps = re.findall(r"<p[^>]*>(.*?)</p>", t, flags=re.S | re.I)
    paras = [strip_html(p) for p in ps]
    paras = [p for p in paras if len(p) >= 15]
    joined = " ".join(paras)
    if len(joined) >= 120:
        return joined[:1600]
    # 退化: 整页去标签, 跳过头 600 字符(导航)取正文
    all_text = strip_html(t)
    if len(all_text) > 600:
        return all_text[600:600 + 1600].strip()
    return all_text[:1600].strip()


def norm_title(t: str) -> str:
    return re.sub(r"\s+", "", (t or "").strip().lower())


def article_id(url: str) -> str:
    return f"WEB_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}"


# ============================ 单源抓取 ============================
def scrape_source(name: str, cfg: dict, exclude_kw: list, global_title_clean: str = ""):
    list_urls = cfg.get("list_urls", [cfg.get("site", "")])
    url_re = re.compile(cfg["url_pattern"])
    t_min = cfg.get("title_min", 8)
    t_max = cfg.get("title_max", 50)
    max_n = cfg.get("max_per_source", 25)
    date_re = re.compile(cfg["date_re"]) if cfg.get("date_re") else None
    title_clean = re.compile(cfg.get("title_clean_re", global_title_clean or "")) if (cfg.get("title_clean_re") or global_title_clean) else None

    seen_links, cands = set(), []
    for lu in list_urls:
        if not lu:
            continue
        try:
            html_text = fetch_html(lu)
        except Exception as e:
            print(f"    [列表页失败] {lu[:50]}: {type(e).__name__}: {str(e)[:60]}")
            continue
        ex = LinkExtractor(lu)
        try:
            ex.feed(html_text)
        except Exception:
            pass
        for txt, href in ex.links:
            if href in seen_links:
                continue
            if not url_re.search(href):
                continue
            t = txt.strip()
            if title_clean:
                t = title_clean.sub("", t).strip()
            if not (t_min <= len(t) <= t_max):
                continue
            if any(k in t for k in exclude_kw):
                continue
            seen_links.add(href)
            cands.append((t, href))

    # 按 URL 日期排序(新→旧), 取前 max_n
    def sort_key(c):
        if date_re:
            m = date_re.search(c[1])
            if m:
                return -int(m.group(1))
        return 0
    cands.sort(key=sort_key)
    cands = cands[:max_n]
    print(f"  [{name}] 列表提取 {len(cands)} 条候选")
    return cands


def main():
    data = json.loads(SRC.read_text(encoding="utf-8"))
    sources = data["sources"]
    exclude_kw = data.get("exclude_keywords", [])
    print(f"=== 官网直抓: {len(sources)} 个源 (SKIP_CONTENT={SKIP_CONTENT}) ===")

    subj, _ = [], []  # 仅占位, 真正软文过滤用 is_soft_article(内部已加载规则)
    fresh = {}
    total_new = total_skip = total_soft = 0
    now = datetime.now(timezone(timedelta(hours=8)))
    now_ts = int(now.timestamp())

    for name, cfg in sources.items():
        print(f"\n--- {name} ({cfg.get('site','')}) ---")
        try:
            cands = scrape_source(name, cfg, exclude_kw)
        except Exception as e:
            print(f"  [源失败] {e}")
            continue
        new = skip = soft = 0
        for title, link in cands:
            # 软文过滤(前置)
            s, reason = is_soft_article(title, "")
            if s:
                soft += 1
                print(f"  [软文拦截] {title[:36]} | {reason}")
                continue
            # 正文
            text = ""
            if not SKIP_CONTENT:
                try:
                    text = extract_content(fetch_html(link, timeout=12))
                except Exception as e:
                    text = ""  # 抓不到正文也不放弃该条, 仅摘要为空
            # 日期
            pub_ts = now_ts
            date_re = re.compile(cfg["date_re"]) if cfg.get("date_re") else None
            if date_re:
                m = date_re.search(link)
                if m:
                    try:
                        d = datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=timezone(timedelta(hours=8)))
                        pub_ts = int(d.timestamp())
                    except Exception:
                        pass
            pub_dt = datetime.fromtimestamp(pub_ts)
            art = {
                "id": article_id(link),
                "title": title[:255],
                "link": link,
                "pub_date": pub_dt.strftime("%Y-%m-%d %H:%M"),
                "content_html": f"<p>{html.escape(text)}</p>" if text else "",
                "text": text,
                "source": name,
                "publish_time": str(pub_ts),
            }
            key = norm_title(title)
            if key in fresh:
                skip += 1
                continue
            fresh[key] = art
            new += 1
            if text:
                print(f"  + {title[:40]:<42} ({len(text)}字)")
            else:
                print(f"  + {title[:40]:<42} (无正文)")
        total_new += new
        total_skip += skip
        total_soft += soft
        print(f"  → 新增 {new} / 跳过 {skip} / 软文 {soft}")
        time.sleep(0.3)
    print(f"\n[web] 本轮抓取: 新增 {total_new} / 软文拦截 {total_soft}")

    # 合并历史
    token = get_token()
    existing = download_existing(token)
    print(f"[web] 云端已有 web 文章: {len(existing)}")
    merged = {norm_title(a.get("title", "")): a for a in existing}
    n_override = 0
    for key, art in fresh.items():
        if key in merged:
            n_override += 1
        merged[key] = art
    print(f"[web] 覆盖更新 {n_override} 篇, 合并后 {len(merged)} 篇")

    cutoff_ts = int((now - timedelta(days=DAYS_KEEP)).timestamp())
    kept = [a for a in merged.values() if (int(a.get("publish_time", "0") or 0)) >= cutoff_ts]
    print(f"[web] 修剪至近 {DAYS_KEEP} 天: 保留 {len(kept)} 篇")

    out = {
        "exported_at": now.strftime("%Y-%m-%d %H:%M"),
        "days": DAYS_KEEP,
        "n_sources": len({a.get("source") for a in kept if a.get("source")}),
        "source": "web",
        "articles": kept,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"[web] 写入 {OUT_JSON} ({OUT_JSON.stat().st_size/1024:.1f} KB)")

    msg = f"data: web-sync {now.strftime('%Y-%m-%d %H:%M')} ({len(kept)}篇)"
    if not push_to_github(OUT_JSON, msg, token):
        sys.exit(1)
    print(f"[web] DONE - {len(kept)} 篇官网直抓已推送至 {REPO_PATH}")


if __name__ == "__main__":
    main()
