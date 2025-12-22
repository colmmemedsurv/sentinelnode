import json
import time
import re
import os
from datetime import datetime, timezone
from email.utils import format_datetime
import requests
import xml.etree.ElementTree as ET

INPUT_JSON = "data/relevant_items.json"
OUTPUT_RSS = "docs/betterdoi.xml"
PUBMED_DEBUG = "data/pubmed_raw.txt"

CROSSREF_API = "https://api.crossref.org/works"
PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

USER_AGENT = "sentinelnode/1.0 (mailto:colmmemedsurv@users.noreply.github.com)"
RATE_LIMIT_SLEEP = 0.4

DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)


# ---------------- Utilities ----------------

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
    return re.sub(r"\s+", " ", text).strip()


def extract_doi(text: str | None) -> str | None:
    if not text:
        return None
    m = DOI_RE.search(text)
    return m.group(0) if m else None


# ---------------- PubMed ----------------

def pubmed_lookup_by_doi(doi: str, debug_fp) -> dict | None:
    try:
        r = requests.get(
            PUBMED_EFETCH,
            params={
                "db": "pubmed",
                "retmode": "xml",
                "id": doi,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )

        debug_fp.write(f"\n===== DOI {doi} =====\n")
        debug_fp.write(r.text + "\n")

        if r.status_code != 200:
            return None

        root = ET.fromstring(r.text)
        article = root.find(".//PubmedArticle")
        if article is None:
            return None

        data = {}

        # ---- Authors ----
        authors = []
        for a in article.findall(".//Author"):
            last = a.findtext("LastName")
            fore = a.findtext("ForeName")
            if last:
                authors.append(f"{fore} {last}".strip())
        if authors:
            data["authors"] = authors

        # ---- Journal ----
        journal = article.findtext(".//Journal/Title")
        if journal:
            data["journal"] = journal

        # ---- Abstract (FULL, concatenated) ----
        abstract_parts = []
        for ab in article.findall(".//AbstractText"):
            label = ab.attrib.get("Label")
            text = (ab.text or "").strip()
            if not text:
                continue
            if label:
                abstract_parts.append(f"{label.capitalize()}: {text}")
            else:
                abstract_parts.append(text)

        if abstract_parts:
            data["abstract"] = "\n\n".join(abstract_parts)

        # ---- Pub date ----
        year = article.findtext(".//PubDate/Year")
        month = article.findtext(".//PubDate/Month") or "01"
        day = article.findtext(".//PubDate/Day") or "01"
        if year:
            dt = datetime(int(year), 1, 1, tzinfo=timezone.utc)
            data["pubDate"] = format_datetime(dt)

        return data

    except Exception as e:
        debug_fp.write(f"ERROR: {e}\n")
        return None


# ---------------- RSS ----------------

def build_rss(items: list[dict]) -> str:
    now = format_datetime(datetime.now(timezone.utc))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">',
        "<channel>",
        "<title>Head &amp; Neck Cancer – DOI-Enriched Feed</title>",
        "<link>https://colmmemedsurv.github.io/sentinelnode/</link>",
        "<description>Curated head &amp; neck cancer literature with Crossref and PubMed enrichment.</description>",
        f"<lastBuildDate>{xml_escape(now)}</lastBuildDate>",
    ]

    for it in items:
        parts.append("<item>")
        parts.append(f"<title>{xml_escape(it.get('title',''))}</title>")

        if it.get("link"):
            parts.append(f"<link>{xml_escape(it['link'])}</link>")
            parts.append(f"<guid isPermaLink='true'>{xml_escape(it['link'])}</guid>")

        if it.get("pubDate"):
            parts.append(f"<pubDate>{xml_escape(it['pubDate'])}</pubDate>")

        if it.get("authors"):
            parts.append(
                f"<dc:creator>{xml_escape(', '.join(it['authors']))}</dc:creator>"
            )

        if it.get("journal"):
            parts.append(
                f"<prism:publicationName>{xml_escape(it['journal'])}</prism:publicationName>"
            )

        if it.get("doi"):
            parts.append(f"<prism:doi>{xml_escape(it['doi'])}</prism:doi>")

        parts.append(
            f"<description>{xml_escape(f'Journal: {it.get('journal','')} | DOI: {it.get('doi','')}')}</description>"
        )

        if it.get("abstract"):
            parts.append("<content:encoded><![CDATA[")
            parts.append(f"<p><strong>Journal</strong>: {it.get('journal','')}</p>")
            if it.get("authors"):
                parts.append(f"<p><strong>Authors</strong>: {', '.join(it['authors'])}</p>")
            if it.get("doi"):
                parts.append(
                    f"<p><strong>DOI</strong>: "
                    f"<a href='https://doi.org/{it['doi']}'>{it['doi']}</a></p>"
                )
            parts.append("<hr/>")
            parts.append("<p><strong>Abstract</strong></p>")
            parts.append(f"<p>{it['abstract'].replace(chr(10), '<br/>')}</p>")
            parts.append("]]></content:encoded>")

        parts.append("</item>")

    parts.append("</channel></rss>")
    return "\n".join(parts)


# ---------------- Main ----------------

def main():
    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        items = json.load(f)

    enriched = []

    with open(PUBMED_DEBUG, "w", encoding="utf-8") as debug_fp:
        for it in items:
            it = dict(it)

            if not it.get("allow_doi_lookup"):
                enriched.append(it)
                continue

            doi = extract_doi(it.get("doi") or "")
            if not doi:
                enriched.append(it)
                continue

            it["doi"] = doi

            pm = pubmed_lookup_by_doi(doi, debug_fp)
            time.sleep(RATE_LIMIT_SLEEP)

            if pm:
                it.setdefault("journal", pm.get("journal"))
                it.setdefault("authors", pm.get("authors"))
                it.setdefault("abstract", pm.get("abstract"))
                it.setdefault("pubDate", pm.get("pubDate"))

            enriched.append(it)

    rss = build_rss(enriched)
    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(rss)


if __name__ == "__main__":
    main()
