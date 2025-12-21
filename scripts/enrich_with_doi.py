import json
import time
import re
from datetime import datetime, timezone
from email.utils import format_datetime

import requests

INPUT_JSON = "data/relevant_items.json"
OUTPUT_RSS = "docs/betterdoi.xml"

CROSSREF_API = "https://api.crossref.org/works/"
USER_AGENT = "sentinelnode/1.0 (mailto:colmmemedsurv@users.noreply.github.com)"
RATE_LIMIT_SLEEP = 0.25


def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )


def strip_xml_tags(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\b[jJ]ats:[A-Za-z0-9_-]+\b", " ", text)
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


def best_pubdate(item: dict, crossref_message: dict | None) -> tuple[str | None, str | None]:
    if item.get("published"):
        return str(item["published"]), None

    if not crossref_message:
        return None, None

    def dt_from_parts(parts):
        try:
            y = int(parts[0][0])
            m = int(parts[0][1]) if len(parts[0]) > 1 else 1
            d = int(parts[0][2]) if len(parts[0]) > 2 else 1
            dt = datetime(y, m, d, tzinfo=timezone.utc)
            return format_datetime(dt), dt.isoformat()
        except Exception:
            return None, None

    for key in ("published-online", "issued", "created"):
        obj = crossref_message.get(key)
        if obj and obj.get("date-parts"):
            return dt_from_parts(obj["date-parts"])

    return None, None


def build_rss(items: list[dict]) -> str:
    now = format_datetime(datetime.now(timezone.utc))

    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append(
        '<rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">'
    )
    parts.append("<channel>")

    parts.append("<title>Head &amp; Neck Cancer – DOI-Enriched Feed</title>")
    parts.append("<link>https://colmmemedsurv.github.io/sentinelnode/</link>")
    parts.append("<description>Curated head &amp; neck cancer literature with DOI-based enrichment.</description>")
    parts.append(f"<lastBuildDate>{xml_escape(now)}</lastBuildDate>")

    for it in items:
        title = it.get("title", "")
        link = it.get("link", "")
        journal = it.get("journal", "")
        doi = it.get("doi_display", "DOI not found")
        authors = it.get("authors", [])
        abstract = it.get("abstract", "")
        pub_rss = it.get("pubDate")
        pub_iso = it.get("pubDateISO")

        parts.append("<item>")
        parts.append(f"<title>{xml_escape(title)}</title>")

        if link:
            parts.append(f"<link>{xml_escape(link)}</link>")
            parts.append(f"<guid isPermaLink='true'>{xml_escape(link)}</guid>")

        if pub_rss:
            parts.append(f"<pubDate>{xml_escape(pub_rss)}</pubDate>")
        if pub_iso:
            parts.append(f"<dc:date>{xml_escape(pub_iso)}</dc:date>")

        if authors:
            parts.append(f"<dc:creator>{xml_escape('; '.join(authors))}</dc:creator>")

        if journal:
            parts.append(f"<prism:publicationName>{xml_escape(journal)}</prism:publicationName>")

        if doi and doi != "DOI not found":
            parts.append(f"<prism:doi>{xml_escape(doi)}</prism:doi>")

        desc = f"Journal: {journal} | DOI: {doi}"
        parts.append(f"<description>{xml_escape(desc)}</description>")

        if abstract:
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

    enriched = []

    for it in items:
        it = dict(it)
        doi = (it.get("doi") or "").strip()
        it["doi_display"] = doi if doi else "DOI not found"

        cr = lookup_doi(doi) if doi else None
        if cr:
            time.sleep(RATE_LIMIT_SLEEP)

        pub_rss, pub_iso = best_pubdate(it, cr)
        it["pubDate"] = pub_rss
        it["pubDateISO"] = pub_iso

        if cr:
            if cr.get("container-title"):
                it["journal"] = cr["container-title"][0]
            if cr.get("author"):
                it["authors"] = [
                    " ".join(filter(None, [a.get("given"), a.get("family")]))
                    for a in cr["author"]
                    if a.get("family")
                ]
            if cr.get("abstract"):
                it["abstract"] = strip_xml_tags(cr["abstract"])
            if cr.get("URL"):
                it["link"] = cr["URL"]

        if it.get("abstract"):
            it["abstract"] = strip_xml_tags(it["abstract"])

        enriched.append(it)

    rss = build_rss(enriched)
    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(rss)


if __name__ == "__main__":
    main()
