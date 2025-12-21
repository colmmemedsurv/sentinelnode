import json
import time
import re
from datetime import datetime, timezone
from email.utils import format_datetime

import requests

INPUT_JSON = "data/relevant_items.json"
OUTPUT_RSS = "docs/betterdoi.xml"

CROSSREF_API = "https://api.crossref.org/works/"

# Set a real contact email if you want (recommended by Crossref etiquette).
# This is not required, but it helps if you ever get rate-limited.
USER_AGENT = "sentinelnode/1.0 (mailto:colmmemedsurv@users.noreply.github.com)"

RATE_LIMIT_SLEEP = 0.25  # polite pacing


def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )


def strip_xml_tags(text: str) -> str:
    """
    Remove XML/JATS tags without generating any new content.
    This is a deterministic transformation: it only removes markup
    and normalizes whitespace.
    """
    if not text:
        return ""

    # Remove XML/HTML tags like <jats:p>...</jats:p>
    text = re.sub(r"<[^>]+>", " ", text)

    # Remove bare 'jats:something' tokens that may appear if tags were partially stripped
    text = re.sub(r"\b[jJ]ats:[A-Za-z0-9_-]+\b", " ", text)

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def lookup_doi(doi: str):
    try:
        r = requests.get(
            CROSSREF_API + doi,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        return r.json().get("message", {})
    except Exception:
        return None


def best_pubdate(item: dict, crossref_message: dict | None) -> str | None:
    """
    Choose a pubDate string WITHOUT inventing dates:
    - Prefer the RSS item's original 'published' if present.
    - Otherwise, if Crossref provides issued/online-published date-parts, convert to RFC2822.
    - Otherwise None.
    """
    # Prefer original feed date if present
    if item.get("published"):
        # Keep the original as-is (it’s already from RSS); many readers accept it.
        return str(item["published"])

    # Try to build an RFC2822 date from Crossref date-parts if available
    if not crossref_message:
        return None

    def dateparts_to_dt(parts):
        # parts is like [[YYYY, MM, DD]] (MM/DD may be missing)
        try:
            y = int(parts[0][0])
            m = int(parts[0][1]) if len(parts[0]) > 1 else 1
            d = int(parts[0][2]) if len(parts[0]) > 2 else 1
            return datetime(y, m, d, 0, 0, 0, tzinfo=timezone.utc)
        except Exception:
            return None

    for key in ("published-online", "issued", "created"):
        obj = crossref_message.get(key)
        if obj and isinstance(obj, dict) and obj.get("date-parts"):
            dt = dateparts_to_dt(obj["date-parts"])
            if dt:
                return format_datetime(dt)

    return None


def normalize_authors_from_crossref(message: dict) -> list[str] | None:
    if not message or "author" not in message:
        return None

    authors = []
    for a in message.get("author", []) or []:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        name = " ".join([p for p in (given, family) if p])
        if name:
            authors.append(name)

    return authors or None


def first_container_title(message: dict) -> str | None:
    if not message:
        return None
    ct = message.get("container-title")
    if isinstance(ct, list) and ct:
        t = str(ct[0]).strip()
        return t or None
    return None


def crossref_url(message: dict) -> str | None:
    if not message:
        return None
    u = message.get("URL")
    if u:
        return str(u).strip() or None
    return None


def build_rss(items: list[dict]) -> str:
    """
    Reader-friendly RSS:
    - description: short metadata line only (journal + DOI)
    - author: separate field
    - content:encoded: abstract/body text (if present)
    """
    now = format_datetime(datetime.now(timezone.utc))

    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">')
    parts.append("<channel>")

    parts.append("<title>Head &amp; Neck Cancer – DOI-Enriched Feed</title>")
    parts.append("<link>https://colmmemedsurv.github.io/sentinelnode/</link>")
    parts.append("<description>Curated head &amp; neck cancer literature. DOI metadata is looked up where available; missing DOIs are marked explicitly.</description>")
    parts.append(f"<lastBuildDate>{xml_escape(now)}</lastBuildDate>")

    for it in items:
        title = (it.get("title") or "").strip()
        link = (it.get("link") or "").strip()
        guid = link if link else (it.get("id") or "")
        journal = (it.get("journal") or "").strip()
        doi_display = (it.get("doi_display") or "DOI not found").strip()
        authors = it.get("authors") or []
        abstract = (it.get("abstract") or "").strip()
        pubdate = (it.get("pubDate") or "").strip()

        parts.append("<item>")

        parts.append(f"<title>{xml_escape(title)}</title>")

        if link:
            parts.append(f"<link>{xml_escape(link)}</link>")
            parts.append(f"<guid isPermaLink='true'>{xml_escape(guid)}</guid>")
        else:
            parts.append(f"<guid isPermaLink='false'>{xml_escape(guid)}</guid>")

        if pubdate:
            parts.append(f"<pubDate>{xml_escape(pubdate)}</pubDate>")

        if authors:
            # RSS 2.0 <author> expects an email, but many readers (incl. Reeder) still display it.
            # We keep it simple; no invented emails.
            parts.append(f"<author>{xml_escape('; '.join([a for a in authors if a]))}</author>")

        # Keep description short + tidy
        desc_bits = []
        if journal:
            desc_bits.append(f"Journal: {journal}")
        desc_bits.append(f"DOI: {doi_display}")
        parts.append(f"<description>{xml_escape(' | '.join(desc_bits))}</description>")

        # Put the abstract into content:encoded so readers render it as the body
        if abstract:
            # No hallucination: abstract is copied from RSS or Crossref and only cleaned (tags stripped)
            parts.append("<content:encoded><![CDATA[")
            parts.append("<p><strong>Abstract</strong></p>")
            parts.append(f"<p>{abstract}</p>")
            parts.append("]]></content:encoded>")

        parts.append("</item>")

    parts.append("</channel>")
    parts.append("</rss>")

    return "\n".join(parts)


def main():
    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        items = json.load(f)

    enriched_items = []

    for it in items:
        it = dict(it)  # copy
        doi = (it.get("doi") or "").strip()

        # Defaults (explicit, no hallucination)
        it["doi_display"] = "DOI not found"

        crossref_message = None
        if doi:
            crossref_message = lookup_doi(doi)
            time.sleep(RATE_LIMIT_SLEEP)

        # PubDate: prefer original RSS published; else Crossref date-parts; else blank
        it["pubDate"] = best_pubdate(it, crossref_message)

        # If DOI exists, display it even if Crossref lookup failed
        if doi:
            it["doi_display"] = doi

        # If Crossref returned data, selectively overwrite fields ONLY when present
        if crossref_message:
            j = first_container_title(crossref_message)
            if j:
                it["journal"] = j

            a = normalize_authors_from_crossref(crossref_message)
            if a:
                it["authors"] = a

            # Abstract from Crossref is often JATS/XML; strip tags deterministically
            cr_abs = crossref_message.get("abstract")
            if cr_abs:
                it["abstract"] = strip_xml_tags(str(cr_abs))

            u = crossref_url(crossref_message)
            if u:
                it["link"] = u

        # If abstract exists but may contain leftover markup from RSS itself, clean it lightly too
        if it.get("abstract"):
            it["abstract"] = strip_xml_tags(str(it["abstract"]))

        enriched_items.append(it)

    rss = build_rss(enriched_items)

    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(rss)


if __name__ == "__main__":
    main()
