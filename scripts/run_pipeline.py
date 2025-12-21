import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import feedparser
from openai import OpenAI

# -----------------------------
# Config
# -----------------------------
OUT_RAW = "data/raw_items.json"
OUT_RELEVANT = "data/relevant_items.json"
OUT_REPORT = "data/run_report.json"
OUT_RSS = "docs/head-neck-cancer.xml"
OUT_INDEX = "docs/index.md"

MODEL = "gpt-4.1-mini"
RATE_LIMIT_SLEEP = 0.15

TOPIC_GUIDANCE = """
Head & neck cancer includes cancers of the oral cavity, oropharynx, hypopharynx, larynx,
nasopharynx, salivary glands, sinonasal tract, thyroid cancer,
head & neck squamous cell carcinoma (HNSCC), HPV-associated oropharyngeal cancer,
and related treatments specific to these.
"""

# -----------------------------
# Helpers
# -----------------------------

def ensure_dirs():
    os.makedirs("data", exist_ok=True)
    os.makedirs("docs", exist_ok=True)

def norm_text(x: Any) -> str:
    return (x or "").strip()

def read_feeds_list(path: str = "feeds.txt") -> List[Dict[str, Any]]:
    """
    Supports per-line annotation:
    [ALLOW_DOI_LOOKUP] https://example.com/rss
    """
    feeds = []

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            allow_doi_lookup = False
            if line.startswith("[ALLOW_DOI_LOOKUP]"):
                allow_doi_lookup = True
                line = line.replace("[ALLOW_DOI_LOOKUP]", "", 1).strip()

            feeds.append({
                "url": line,
                "allow_doi_lookup": allow_doi_lookup
            })

    return feeds

def parse_date(entry: Dict[str, Any]) -> Optional[str]:
    for key in ("published", "updated"):
        if entry.get(key):
            return str(entry[key])
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            try:
                dt = datetime(*val[:6], tzinfo=timezone.utc)
                return dt.strftime("%a, %d %b %Y %H:%M:%S %z")
            except Exception:
                pass
    return None

def extract_authors(entry: Dict[str, Any]) -> List[str]:
    authors = []
    for a in entry.get("authors", []) or []:
        if a.get("name"):
            authors.append(a["name"].strip())
    if not authors and entry.get("author"):
        authors = [entry["author"].strip()]
    return authors

def extract_doi(entry: Dict[str, Any]) -> Optional[str]:
    candidates = []
    for key in ("dc_identifier", "doi", "prism_doi"):
        if entry.get(key):
            candidates.append(str(entry[key]))

    for link in entry.get("links", []) or []:
        href = link.get("href")
        if href and "doi.org/" in href:
            candidates.append(href)

    for key in ("id", "guid"):
        if entry.get(key):
            candidates.append(str(entry[key]))

    doi_re = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
    for c in candidates:
        m = doi_re.search(c)
        if m:
            return m.group(0)
    return None

def extract_abstract(entry: Dict[str, Any]) -> Optional[str]:
    if entry.get("summary"):
        return str(entry["summary"])
    content = entry.get("content")
    if isinstance(content, list) and content:
        return content[0].get("value")
    if entry.get("description"):
        return str(entry["description"])
    return None

def extract_link(entry: Dict[str, Any]) -> Optional[str]:
    if entry.get("link"):
        return str(entry["link"])
    for link in entry.get("links", []) or []:
        if link.get("rel") == "alternate" and link.get("href"):
            return link["href"]
    return None

def extract_journal_title(feed: feedparser.FeedParserDict) -> Optional[str]:
    return norm_text(feed.feed.get("title"))

# -----------------------------
# Classifier
# -----------------------------

def classify_item(client: OpenAI, title: str, abstract: str) -> str:
    text = f"TITLE:\n{title}\n\nABSTRACT:\n{abstract}\n"
    resp = client.responses.create(
        model=MODEL,
        input=(
            "You are a medical RSS relevance classifier.\n"
            "Reply ONLY YES, NO, or UNCERTAIN.\n\n"
            f"{TOPIC_GUIDANCE}\n\n{text}"
        ),
    )
    out = (resp.output_text or "").strip().upper()
    return out if out in {"YES", "NO", "UNCERTAIN"} else "UNCERTAIN"

# -----------------------------
# Deduplication
# -----------------------------

def deduplicate_items(items):
    seen_doi, seen_link, seen_title = set(), set(), set()
    unique = []

    for it in items:
        doi = (it.get("doi") or "").lower()
        link = (it.get("link") or "").lower()
        title = (it.get("title") or "").lower()

        if doi and doi in seen_doi:
            continue
        if link and link in seen_link:
            continue
        if title and title in seen_title:
            continue

        if doi:
            seen_doi.add(doi)
        if link:
            seen_link.add(link)
        if title:
            seen_title.add(title)

        unique.append(it)

    return unique

# -----------------------------
# Main
# -----------------------------

def main():
    ensure_dirs()

    feeds = read_feeds_list()
    if not feeds:
        raise RuntimeError("feeds.txt is empty")

    raw_items = []
    feed_counts = {}
    feed_titles = {}

    for feed in feeds:
        url = feed["url"]
        parsed = feedparser.parse(url)

        feed_counts[url] = len(parsed.entries or [])
        feed_titles[url] = extract_journal_title(parsed) or ""

        for entry in parsed.entries or []:
            raw_items.append({
                "id": str(uuid.uuid4()),
                "source_feed": url,
                "journal": extract_journal_title(parsed),
                "title": norm_text(entry.get("title")),
                "abstract": extract_abstract(entry),
                "published": parse_date(entry),
                "authors": extract_authors(entry),
                "doi": extract_doi(entry),
                "link": extract_link(entry),
                "allow_doi_lookup": feed["allow_doi_lookup"],
                "raw_entry_keys": sorted(entry.keys()),
            })

    with open(OUT_RAW, "w", encoding="utf-8") as f:
        json.dump(raw_items, f, indent=2, ensure_ascii=False)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    client = OpenAI(api_key=api_key)

    relevant = []
    decisions = {"YES": 0, "NO": 0, "UNCERTAIN": 0}

    for it in raw_items:
        title = it.get("title") or ""
        abstract = it.get("abstract") or ""
        if len((title + abstract).strip()) < 20:
            decision = "UNCERTAIN"
        else:
            decision = classify_item(client, title, abstract)

        it["relevance"] = decision
        decisions[decision] += 1
        if decision == "YES":
            relevant.append(it)

        time.sleep(RATE_LIMIT_SLEEP)

    with open(OUT_RELEVANT, "w", encoding="utf-8") as f:
        json.dump(relevant, f, indent=2, ensure_ascii=False)

    relevant = deduplicate_items(relevant)

    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "feeds": [
            {
                "url": f["url"],
                "feed_title": feed_titles.get(f["url"], ""),
                "items_in_feed": feed_counts.get(f["url"], 0),
                "relevant_yes": sum(
                    1 for it in relevant if it["source_feed"] == f["url"]
                ),
            }
            for f in feeds
        ],
        "decisions_total": decisions,
    }

    with open(OUT_REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    with open(OUT_INDEX, "w", encoding="utf-8") as f:
        f.write("# SentinelNode\n\n")
        f.write("Curated RSS feed: **head-neck-cancer.xml**\n")

if __name__ == "__main__":
    main()
