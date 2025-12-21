import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import feedparser
from dateutil import parser as dateparser
from openai import OpenAI

# -----------------------------
# Config
# -----------------------------
OUT_RAW = "data/raw_items.json"
OUT_RELEVANT = "data/relevant_items.json"
OUT_REPORT = "data/run_report.json"
OUT_RSS = "docs/head-neck-cancer.xml"
OUT_INDEX = "docs/index.md"

MODEL = "gpt-4.1-mini"  # classifier only (YES/NO/UNCERTAIN)
RATE_LIMIT_SLEEP = 0.15  # gentle pacing; adjust if needed

# Head & neck cancer topic definition (for classification prompt)
TOPIC_GUIDANCE = """
Head & neck cancer includes cancers of the oral cavity, oropharynx, hypopharynx, larynx,
nasopharynx, salivary glands, sinonasal tract, thyroid cancer,
head & neck squamous cell carcinoma (HNSCC), HPV-associated oropharyngeal cancer, head and neck skin cancers,
and related treatments/diagnostics (surgery, radiotherapy, chemotherapy, immunotherapy, targeted therapy) specific to these.
Exclude: non-head/neck sites unless clearly metastatic to head/neck or the study is explicitly about head/neck oncology.

- PRIMARY focus: Head and neck squamous cell carcinoma (HNSCC)
  (oropharynx, larynx, hypopharynx, nasopharynx, oral cavity)
- SECONDARY topics: thyroid cancers and salivary gland cancers
  (ACC, MEC, salivary duct carcinoma), head and neck skin cancer. rare head and neck tumors.
  
"""

# -----------------------------
# Helpers: safe extraction only
# -----------------------------

def read_feeds_list(path: str = "feeds.txt") -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]

def ensure_dirs():
    os.makedirs("data", exist_ok=True)
    os.makedirs("docs", exist_ok=True)
    os.makedirs("scripts", exist_ok=True)

def norm_text(x: Any) -> str:
    return (x or "").strip()

def parse_date(entry: Dict[str, Any]) -> Optional[str]:
    # Only use dates provided by feedparser fields (no guessing)
    for key in ("published", "updated"):
        val = entry.get(key)
        if val:
            return str(val)
    # sometimes structured time exists
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
        name = (a.get("name") or "").strip()
        if name:
            authors.append(name)
    # some feeds use "author" as a string
    if not authors:
        s = norm_text(entry.get("author"))
        if s:
            authors = [s]
    return authors

def extract_doi(entry: Dict[str, Any]) -> Optional[str]:
    # Strict: DOI only if explicitly present in feed fields.
    candidates = []

    # common fields
    for key in ("dc_identifier", "doi", "prism_doi"):
        v = entry.get(key)
        if v:
            candidates.append(str(v))

    # links sometimes contain doi.org
    for link in entry.get("links", []) or []:
        href = link.get("href")
        if href and "doi.org/" in href:
            candidates.append(href)

    # sometimes in id/guid
    for key in ("id", "guid"):
        v = entry.get(key)
        if v:
            candidates.append(str(v))

    doi_re = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
    for c in candidates:
        m = doi_re.search(c)
        if m:
            return m.group(0)
    return None

def extract_abstract(entry: Dict[str, Any]) -> Optional[str]:
    # RSS often puts abstract in summary/description/content
    if entry.get("summary"):
        return str(entry.get("summary"))
    # Atom content blocks
    content = entry.get("content")
    if isinstance(content, list) and content:
        v = content[0].get("value")
        if v:
            return str(v)
    # sometimes description
    if entry.get("description"):
        return str(entry.get("description"))
    return None

def extract_link(entry: Dict[str, Any]) -> Optional[str]:
    if entry.get("link"):
        return str(entry["link"])
    # fallback: first alternate link
    for link in entry.get("links", []) or []:
        if link.get("rel") == "alternate" and link.get("href"):
            return str(link["href"])
    return None

def extract_journal_title(feed: feedparser.FeedParserDict) -> Optional[str]:
    return norm_text(feed.feed.get("title"))

# -----------------------------
# OpenAI classifier (no generation)
# -----------------------------

def classify_item(client: OpenAI, title: str, abstract: str) -> str:
    """
    Returns one of: YES / NO / UNCERTAIN
    """
    text = f"TITLE:\n{title}\n\nABSTRACT_OR_SUMMARY_FROM_RSS:\n{abstract}\n"
    resp = client.responses.create(
        model=MODEL,
        input=(
            "You are a medical RSS relevance classifier.\n"
            "Task: decide if the item is relevant to head & neck cancer.\n"
            "Rules:\n"
            "- Answer with ONLY one token: YES, NO, or UNCERTAIN.\n"
            "- Do not add explanations.\n"
            "- Use ONLY the provided RSS text.\n\n"
            f"TOPIC GUIDANCE:\n{TOPIC_GUIDANCE}\n\n"
            f"ITEM:\n{text}"
        ),
    )
    out = (resp.output_text or "").strip().upper()
    if out not in {"YES", "NO", "UNCERTAIN"}:
        return "UNCERTAIN"
    return out

# -----------------------------
# RSS XML generator (simple RSS2)
# -----------------------------

def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )

def build_rss(items: List[Dict[str, Any]]) -> str:
    # minimal RSS 2.0
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<rss version="2.0">')
    parts.append("<channel>")
    parts.append("<title>Head &amp; Neck Cancer – Curated Journal Feed</title>")
    parts.append("<link>https://colmmemedsurv.github.io/sentinelnode/</link>")
    parts.append("<description>Automatically curated from selected journal RSS feeds. Metadata is copied only when present in the source feed.</description>")
    parts.append(f"<lastBuildDate>{xml_escape(now)}</lastBuildDate>")

    for it in items:
        title = xml_escape(norm_text(it.get("title")))
        link = xml_escape(norm_text(it.get("link")))
        pub = xml_escape(norm_text(it.get("published") or ""))
        journal = xml_escape(norm_text(it.get("journal") or ""))
        doi = xml_escape(norm_text(it.get("doi") or ""))
        authors = it.get("authors") or []
        authors_str = xml_escape(", ".join([a for a in authors if a]))

        abstract = it.get("abstract")
        abstract_str = xml_escape(abstract) if abstract else ""

        parts.append("<item>")
        parts.append(f"<title>{title}</title>")
        if link:
            parts.append(f"<link>{link}</link>")
            parts.append(f"<guid isPermaLink='true'>{link}</guid>")
        else:
            # if no link, use deterministic guid
            parts.append(f"<guid isPermaLink='false'>{xml_escape(it.get('id',''))}</guid>")
        if pub:
            parts.append(f"<pubDate>{pub}</pubDate>")

        # Put everything that exists into description (still only copied)
        desc_bits = []
        if journal:
            desc_bits.append(f"Journal: {journal}")
        if doi:
            desc_bits.append(f"DOI: {doi}")
        if authors_str:
            desc_bits.append(f"Authors: {authors_str}")
        if abstract_str:
            desc_bits.append(f"\n\nAbstract/summary (from RSS):\n{abstract_str}")
        description = xml_escape("\n".join(desc_bits)) if desc_bits else ""
        parts.append(f"<description>{description}</description>")
        parts.append("</item>")

    parts.append("</channel>")
    parts.append("</rss>")
    return "\n".join(parts)

# -----------------------------
# Main
# -----------------------------

def main():
    ensure_dirs()

    feeds = read_feeds_list()
    if not feeds:
        raise RuntimeError("feeds.txt is empty. Add RSS URLs, one per line.")

    # Fetch all feeds
    raw_items: List[Dict[str, Any]] = []
    feed_counts: Dict[str, int] = {}
    feed_titles: Dict[str, str] = {}

    for url in feeds:
        parsed = feedparser.parse(url)
        feed_counts[url] = len(parsed.entries or [])
        feed_titles[url] = extract_journal_title(parsed) or ""
        for entry in (parsed.entries or []):
            item = {
                "id": str(uuid.uuid4()),
                "source_feed": url,
                "journal": extract_journal_title(parsed),
                "title": norm_text(entry.get("title")),
                "abstract": extract_abstract(entry),  # only copied if present
                "published": parse_date(entry),       # only if present
                "authors": extract_authors(entry),    # only if present
                "doi": extract_doi(entry),            # only if present
                "link": extract_link(entry),          # only if present
                "raw_entry_keys": sorted(list(entry.keys())),
            }
            raw_items.append(item)

    with open(OUT_RAW, "w", encoding="utf-8") as f:
        json.dump(raw_items, f, indent=2, ensure_ascii=False)

    # Classify
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in GitHub Secrets.")

    client = OpenAI(api_key=api_key)

    relevant: List[Dict[str, Any]] = []
    decisions = {"YES": 0, "NO": 0, "UNCERTAIN": 0}

    for it in raw_items:
        title = it.get("title") or ""
        abstract = it.get("abstract") or ""
        # If there is basically no text, mark UNCERTAIN (don’t guess)
        if len((title + " " + abstract).strip()) < 20:
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

    # Report
    per_feed_relevant: Dict[str, int] = {u: 0 for u in feeds}
    for it in relevant:
        per_feed_relevant[it["source_feed"]] = per_feed_relevant.get(it["source_feed"], 0) + 1

    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "feeds": [
            {
                "url": u,
                "feed_title": feed_titles.get(u, ""),
                "items_in_feed": feed_counts.get(u, 0),
                "relevant_yes": per_feed_relevant.get(u, 0),
            }
            for u in feeds
        ],
        "decisions_total": decisions,
        "notes": [
            "No hallucination: metadata is copied only when present in the RSS feed.",
            "If abstract/authors/doi are missing in RSS, they remain empty.",
            "Classifier outputs only YES/NO/UNCERTAIN."
        ],
    }
    with open(OUT_REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # Write RSS + docs index
    rss_xml = build_rss(relevant)
    with open(OUT_RSS, "w", encoding="utf-8") as f:
        f.write(rss_xml)

    with open(OUT_INDEX, "w", encoding="utf-8") as f:
        f.write("# SentinelNode\n\n")
        f.write("Curated RSS feed (head & neck cancer): **head-neck-cancer.xml**\n\n")
        f.write("- Feed: `head-neck-cancer.xml`\n")
        f.write("- This site updates daily via GitHub Actions.\n")

if __name__ == "__main__":
    main()
