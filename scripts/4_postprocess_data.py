"""
Step 4: 原始数据后处理脚本 (4_postprocess_data.py)
读取 data/raw/ 下的三个原始批次文件（保持 data/raw/ 100% 原样不动），
针对前两类确定性工程问题进行修复，并输出至 data/postprocessed/：

1. 修复问题 1（发布时间 published_at 格式与时区不统一）：
   - 将 RFC 2822 (如 'Mon, 02 Nov 2026 23:13:53 +0000'、'+0800'、'GMT') 与 ISO 8601 格式
     全部统一转换为标准 UTC ISO 8601 格式 ('YYYY-MM-DDTHH:MM:SSZ')，
     同时保留原始字符串到 'published_at_raw' 字段备查。

2. 修复问题 2（极客公园早报合集长文因段落数/长度截断导致标题与正文不对应）：
   - 针对极客公园多合一早报条目（ITEM-20261102-005、ITEM-20261103-026、ITEM-20261104-005）
     及所有极客公园长文，重新抓取完整网页正文（解除前 12 段与 1800 字符截断限制，提取全部小标题与段落），
     确保标题中提及的“马斯克回应 Grok Bot”、“谷歌 AI 切断流量”、“ChatGPT 搜索量达谷歌 20%”等内容完整包含在正文中。

3. 附带清理固定网页内嵌横幅噪声：
   - 剥离正常 TechCrunch AI 报道正文中固定穿插的 2 行波士顿峰会买票促销横幅（保留 3 篇独立峰会广告文章本身用于测试后续 AI 软文/广告识别）。
"""

import json
import os
import re
import sys
import html
import ssl
import urllib.request
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}

# TechCrunch 正文中穿插的固定峰会买票横幅正则
TC_INLINE_BANNER_PATTERNS = [
    r"Buy One Founder Summit Ticket, Get a Second for Free\.\s*Register now\.",
    r"Founder Summit begins Nov\. 4, 9:00 a\.m\. ET\.\s*Get your pass savings & deals before it starts\.\s*REGISTER HERE\.",
    r"Buy One Pass, Get a Second Free Cover more ground at TechCrunch’s ultimate founder bootcamp of the year.*",
]

# 3 篇独立的峰会门票广告文章 ID（不剥离其内容，留给后续模块识别为 EVENT_AD）
STANDALONE_AD_IDS = {"ITEM-20261102-026", "ITEM-20261103-008", "ITEM-20261103-009"}


def normalize_timestamp(raw_str, fallback_date):
    """将不同来源的时间格式统一转换为标准 ISO 8601 UTC 格式 (YYYY-MM-DDTHH:MM:SSZ)"""
    if not raw_str:
        return f"{fallback_date}T00:00:00Z"
    s = raw_str.strip()

    # 1. 尝试解析 RFC 2822 (例如: 'Mon, 02 Nov 2026 23:13:53 +0000' 或 '+0800' 或 'GMT')
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass

    # 2. 尝试解析 ISO 8601 (例如: '2026-11-02T20:26:52Z' 或带时区偏移)
    try:
        iso_s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass

    return f"{fallback_date}T00:00:00Z"


def clean_text(raw_html):
    if not raw_html:
        return ""
    text = html.unescape(raw_html)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_full_geekpark_article(url):
    """针对极客公园早报/长文，提取完整的小标题 (h2/h3/h4/strong) 与全部段落 (<p>)，不设 12 段截断"""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
        page_html = resp.read().decode("utf-8", errors="replace")

    cleaned = re.sub(r"<(script|style|nav|footer|header|aside|noscript).*?</\1>", " ", page_html, flags=re.DOTALL | re.IGNORECASE)

    # 提取 article-content 或整页中的标题与段落块
    blocks = []
    for m in re.finditer(r"<(h1|h2|h3|h4|p)[^>]*>(.*?)</\1>", cleaned, flags=re.DOTALL | re.IGNORECASE):
        tag = m.group(1).lower()
        txt = clean_text(m.group(2))
        if not txt:
            continue
        if tag in ("h1", "h2", "h3", "h4") and len(txt) >= 4:
            blocks.append(f"\n【{txt}】")
        elif tag == "p" and len(txt) >= 15:
            if not re.search(r"(Copyright|All Rights Reserved|极客公园版权所有|扫码关注)", txt, re.IGNORECASE):
                blocks.append(txt)

    full_text = "\n".join(blocks).strip()
    return full_text


def clean_tc_inline_banners(content, item_id):
    if item_id in STANDALONE_AD_IDS:
        return content
    cleaned = content
    for pat in TC_INLINE_BANNER_PATTERNS:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def process_batches():
    os.makedirs("data/postprocessed", exist_ok=True)
    batch_files = [
        "batch_1_2026-11-02.json",
        "batch_2_2026-11-03.json",
        "batch_3_2026-11-04.json",
    ]

    postprocess_report = {
        "timestamp_normalized_count": 0,
        "geekpark_fulltext_repaired": [],
        "tc_banner_cleaned_count": 0
    }

    for fname in batch_files:
        in_path = os.path.join("data/raw", fname)
        out_path = os.path.join("data/postprocessed", fname)

        with open(in_path, "r", encoding="utf-8") as f:
            items = json.load(f)

        processed_items = []
        for it in items:
            new_it = dict(it)
            item_id = new_it["item_id"]

            # 0. 清理零宽不可见字符 (\u200b 等)
            new_it["title"] = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", new_it.get("title", "")).strip()
            new_it["content"] = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", new_it.get("content", "")).strip()

            # 1. 统一转换 published_at 为 ISO 8601 UTC 标准格式
            raw_pub = new_it.get("published_at", "")
            norm_pub = normalize_timestamp(raw_pub, new_it.get("published_date", "2026-11-02"))
            new_it["published_at_raw"] = raw_pub
            new_it["published_at"] = norm_pub
            postprocess_report["timestamp_normalized_count"] += 1

            # 2. 修复极客公园长文/早报截断遗漏问题
            if new_it.get("source") == "极客公园":
                try:
                    old_len = len(new_it.get("content", ""))
                    full_gp_text = fetch_full_geekpark_article(new_it["url"])
                    if len(full_gp_text) > old_len:
                        new_it["content"] = full_gp_text
                        new_it["enrich_status"] = "postprocessed_full_digest_repaired"
                        postprocess_report["geekpark_fulltext_repaired"].append({
                            "item_id": item_id,
                            "title": new_it["title"],
                            "old_length": old_len,
                            "new_length": len(full_gp_text)
                        })
                except Exception as e:
                    print(f"  [WARN] Failed to re-fetch GeekPark {item_id}: {e}")

            # 3. 清理 TechCrunch 正常报道内嵌的固定峰会买票广告词
            if new_it.get("source") == "TechCrunch AI" and item_id not in STANDALONE_AD_IDS:
                old_c = new_it.get("content", "")
                new_c = clean_tc_inline_banners(old_c, item_id)
                if len(new_c) != len(old_c):
                    new_it["content"] = new_c
                    postprocess_report["tc_banner_cleaned_count"] += 1

            processed_items.append(new_it)

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(processed_items, f, ensure_ascii=False, indent=2)
        print(f"[Saved] {out_path} ({len(processed_items)} items)")

    with open("data/postprocessed/postprocess_audit_log.json", "w", encoding="utf-8") as f:
        json.dump(postprocess_report, f, ensure_ascii=False, indent=2)

    print("\n=== 后处理修复完成 ===")
    print(f"1. 时间戳统一标准化 (ISO 8601 UTC): {postprocess_report['timestamp_normalized_count']} 条")
    print(f"2. 极客公园长文/早报截断补全: {len(postprocess_report['geekpark_fulltext_repaired'])} 条")
    for rep in postprocess_report["geekpark_fulltext_repaired"]:
        print(f"   - {rep['item_id']}: {rep['old_length']} 字符 -> {rep['new_length']} 字符 | {rep['title'][:40]}...")
    print(f"3. TechCrunch 内嵌买票横幅剥离: {postprocess_report['tc_banner_cleaned_count']} 条")


if __name__ == "__main__":
    process_batches()
