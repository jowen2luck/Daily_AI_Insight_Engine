"""
Step 1: 统一多源资讯采集脚本 (1_crawl_raw.py)
覆盖四类信源：
1. 科技媒体 (中/英): TechCrunch AI (含分页), The Verge AI, Ars Technica, 量子位 QbitAI (含分页), 极客公园, Solidot, OSChina 开源中国, 少数派 SSPai
2. 官方渠道: OpenAI Official Blog, HuggingFace Blog, arXiv (cs.AI / cs.CL), GitHub Releases (vLLM, Ollama, LangChain, llama.cpp, Open-WebUI)
3. 聚合与社区平台: Hacker News (Algolia Search API)

输出：
- data/raw_probe/candidates_dump.json (全量原始抓取池)
- data/raw_probe/three_days_candidates.json (按 2026-11-02 ~ 2026-11-04 切分的三天原始候选池)
"""

import urllib.request
import ssl
import json
import xml.etree.ElementTree as ET
import re
import os
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, application/json, text/xml, */*"
}

RSS_SOURCES = [
    # 英文科技媒体
    ("TechCrunch AI", "科技媒体", "en", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("TechCrunch AI", "科技媒体", "en", "https://techcrunch.com/category/artificial-intelligence/feed/?paged=2"),
    ("The Verge AI", "科技媒体", "en", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("Ars Technica AI", "科技媒体", "en", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    # 中文科技媒体
    ("量子位 QbitAI", "科技媒体", "zh", "https://www.qbitai.com/feed"),
    ("量子位 QbitAI", "科技媒体", "zh", "https://www.qbitai.com/feed?paged=2"),
    ("量子位 QbitAI", "科技媒体", "zh", "https://www.qbitai.com/feed?paged=3"),
    ("量子位 QbitAI", "科技媒体", "zh", "https://www.qbitai.com/feed?paged=4"),
    ("极客公园", "科技媒体", "zh", "https://www.geekpark.net/rss"),
    ("Solidot", "科技媒体", "zh", "https://www.solidot.org/index.rss"),
    ("OSChina 开源中国", "科技媒体", "zh", "https://www.oschina.net/news/rss"),
    ("少数派 SSPai", "科技媒体", "zh", "https://sspai.com/feed"),
    # 官方渠道
    ("OpenAI Official Blog", "官方渠道", "en", "https://openai.com/news/rss.xml"),
    ("HuggingFace Blog", "官方渠道", "en", "https://huggingface.co/blog/feed.xml"),
    ("arXiv cs.AI/cs.CL", "官方渠道", "en", "http://export.arxiv.org/api/query?search_query=cat:cs.AI+OR+cat:cs.CL&sortBy=submittedDate&sortOrder=descending&max_results=25"),
]

HN_QUERIES = ["OpenAI", "Anthropic", "Claude", "LLM", "AI", "GPT", "DeepSeek"]

GITHUB_REPOS = [
    "ollama/ollama",
    "vllm-project/vllm",
    "langchain-ai/langchain",
    "ggml-org/llama.cpp",
    "open-webui/open-webui"
]

TARGET_DATES = ["2026-11-02", "2026-11-03", "2026-11-04"]


def fetch_url(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean_html(raw_html):
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    return re.sub(r"\s+", " ", text).strip()


def parse_date(date_str):
    if not date_str:
        return None
    date_str = date_str.strip()
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    m = re.search(r"(202\d-\d{2}-\d{2})", date_str)
    if m:
        return m.group(1)
    return None


def parse_rss_or_atom(xml_text, source_name, source_type, lang, fetched_at):
    items = []
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        print(f"  [WARN] XML parse error for {source_name}: {e}")
        return items

    # RSS 2.0 <item>
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = clean_html(item.findtext("description") or "")
        content_encoded = clean_html(item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")
        body = content_encoded if len(content_encoded) > len(desc) else desc
        pub_date = (item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or "").strip()
        if title and link:
            items.append({
                "source_name": source_name,
                "source_type": source_type,
                "lang": lang,
                "title": title,
                "summary": body[:1200],
                "url": link,
                "published_raw": pub_date,
                "fetched_at": fetched_at
            })

    # Atom <entry>
    atom_ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.findall(f".//{atom_ns}entry"):
        title_el = entry.find(f"{atom_ns}title")
        summary_el = entry.find(f"{atom_ns}summary")
        content_el = entry.find(f"{atom_ns}content")
        body_el = content_el if (content_el is not None and content_el.text) else summary_el
        pub_el = entry.find(f"{atom_ns}published")
        if pub_el is None:
            pub_el = entry.find(f"{atom_ns}updated")

        link = ""
        for link_el in entry.findall(f"{atom_ns}link"):
            href = link_el.attrib.get("href", "")
            rel = link_el.attrib.get("rel", "alternate")
            if rel == "alternate" and href:
                link = href
                break
            elif href and not link:
                link = href

        title = clean_html(title_el.text if title_el is not None else "")
        desc = clean_html(body_el.text if body_el is not None else "")
        pub_date = (pub_el.text if pub_el is not None else "").strip()
        if title and link:
            items.append({
                "source_name": source_name,
                "source_type": source_type,
                "lang": lang,
                "title": title,
                "summary": desc[:1200],
                "url": link,
                "published_raw": pub_date,
                "fetched_at": fetched_at
            })
    return items


def crawl_all():
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_items = []
    seen_urls = set()

    # 1. RSS / Atom 源
    for name, stype, lang, url in RSS_SOURCES:
        print(f"[RSS] Fetching {name} ({url}) ...")
        try:
            xml_text = fetch_url(url)
            parsed = parse_rss_or_atom(xml_text, name, stype, lang, fetched_at)
            added = 0
            for it in parsed:
                if it["url"] not in seen_urls:
                    seen_urls.add(it["url"])
                    all_items.append(it)
                    added += 1
            print(f"  -> Added {added} unique items")
        except Exception as e:
            print(f"  -> [FAIL] {name}: {e}")

    # 2. Hacker News Algolia API
    print("[API] Fetching Hacker News stories ...")
    hn_added = 0
    for q in HN_QUERIES:
        try:
            url = f"https://hn.algolia.com/api/v1/search_by_date?query={q}&tags=story&numericFilters=points>10&hitsPerPage=30"
            data = json.loads(fetch_url(url))
            for hit in data.get("hits", []):
                title = hit.get("title") or ""
                item_url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                points = hit.get("points", 0)
                num_comments = hit.get("num_comments", 0)
                story_text = clean_html(hit.get("story_text") or "")
                summary = f"[HN {points} pts, {num_comments} comments] {story_text if story_text else title} (HN discussion: https://news.ycombinator.com/item?id={hit.get('objectID')})"
                all_items.append({
                    "source_name": "Hacker News",
                    "source_type": "聚合平台",
                    "lang": "en",
                    "title": title,
                    "summary": summary[:1200],
                    "url": item_url,
                    "published_raw": hit.get("created_at", ""),
                    "hn_object_id": hit.get("objectID", ""),
                    "fetched_at": fetched_at
                })
                hn_added += 1
        except Exception as e:
            print(f"  -> [FAIL] HN query {q}: {e}")
    print(f"  -> Added {hn_added} Hacker News items")

    # 3. GitHub Releases API
    print("[API] Fetching GitHub Releases ...")
    gh_added = 0
    for repo in GITHUB_REPOS:
        try:
            gh_url = f"https://api.github.com/repos/{repo}/releases?per_page=5"
            releases = json.loads(fetch_url(gh_url))
            for rel in releases:
                html_url = rel.get("html_url", "")
                if not html_url or html_url in seen_urls:
                    continue
                seen_urls.add(html_url)
                title = f"{repo} Release {rel.get('tag_name', '')}: {rel.get('name', '')}"
                body = clean_html(rel.get("body") or "")
                all_items.append({
                    "source_name": f"GitHub Releases ({repo})",
                    "source_type": "官方渠道",
                    "lang": "en",
                    "title": title,
                    "summary": body[:1200],
                    "url": html_url,
                    "published_raw": rel.get("published_at", ""),
                    "fetched_at": fetched_at
                })
                gh_added += 1
        except Exception as e:
            print(f"  -> [FAIL] GitHub {repo}: {e}")
    print(f"  -> Added {gh_added} GitHub Release items")

    os.makedirs("data/raw_probe", exist_ok=True)
    with open("data/raw_probe/candidates_dump.json", "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)

    three_days = {d: [] for d in TARGET_DATES}
    for it in all_items:
        d = parse_date(it.get("published_raw", ""))
        if d in three_days:
            it["published_date"] = d
            three_days[d].append(it)

    with open("data/raw_probe/three_days_candidates.json", "w", encoding="utf-8") as f:
        json.dump(three_days, f, ensure_ascii=False, indent=2)

    print("\n=== Crawl Complete ===")
    for d in TARGET_DATES:
        print(f"  {d}: {len(three_days[d])} items")


if __name__ == "__main__":
    crawl_all()
