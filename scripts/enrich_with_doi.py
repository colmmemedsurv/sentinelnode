import json
import time
import re
import os
from datetime import datetime, timezone
from email.utils import format_datetime
import xml.etree.ElementTree as ET

import requests

INPUT_JSON = "data/relevant_items.json"
OUTPUT_RSS = "docs/betterdoi.xml"
PUBMED_DEBUG = "docs/pubmed_raw_debug.txt"

CROSSREF_API = "https://api.crossref.org/works/"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

USER_AGENT = "sentinelnode/1.0 (mailto:colmmemedsurv@users.noreply.github.com)"
RATE_LIMIT_SLEEP = 0.34

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
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def extract_doi(text: str | None) -> str | None:
    if not text:
        return None
    m = DOI_RE.search(text)
    return m.group(0) if m else None


# ---------------- Crossref ----------------

def crossref_lookup(doi: str) -> dict | None:
    try:
        r = requests.get(
            CROSSREF_API + doi,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        return r.json().get("message")
    except Exception:
        return None


# ---------------- PubMed ----------------

def pubmed_fetch_by_doi(doi: str) -> dict | None:
    """Return dict with abstract, authors, journal, pubdate"""
    try:
        search = requests.get(
            PUBMED_SEARCH,
            params={
                "db": "pubmed",
                "term": f"{doi}[DOI]",
                "retmode": "json",
            },
            timeout=20,
        )
        ids = search.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return None

        fetch = requests.get(
            PUBMED_FETCH,
            params={
                "db": "pubmed",
                "id": ids[0],
                "retmode": "xml",
            },
            timeout=20,
        )

        raw_xml = fetch.text
        with open(PUBMED_DEBUG, "a", encoding="utf-8") as dbg:
            dbg.write(f"\n===== DOI {doi} =====\n")
            dbg.write(raw_xml)

        root = ET.fromstring(raw_xml)
        article = root.find(".//Article")
        if article is None:
            return None

        # ---- Abstract (FULL, multi-section) ----
        abstract_parts = []
        for node in article.findall(".//AbstractText"):
            label = node.attrib.get("Label")
            txt = strip_xml_tags("".join(node.itertext()))
            if label:
                abstract_parts.append(f"{label}: {txt}")
            else:
                abstract_parts.append(txt)

        abstract = "\n\n".join(abstract_parts).strip()

        # ---- Authors ----
        authors = []
        for a in article.findall(".//Author"):
            last = a.findtext("LastName")
            fore = a.findtext("ForeName")
            if last:
                authors.append(" ".join(filter(None, [fore, last])))

        # ---- Journal ----
        journal = article.findtext(".//Journal/Title")

        return {
            "abstract": abstract or None,
            "authors": authors or None,
            "journal": journal or None,
        }

    except Exception:
        return None


# ---------------- RSS Builder ----------------

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
            parts.append(f"<dc:creator>{xml_escape('; '.join(it['authors']))}</dc:creator>")

        if it.get("journal"):
            parts.append(f"<prism:publicationName>{xml_escape(it['journal'])}</prism:publicationName>")

        if it.get("doi"):
            parts.append(f"<prism:doi>{xml_escape(it['doi'])}</prism:doi>")

        parts.append(
            f"<description>{xml_escape('Journal: ' + (it.get('journal') or '') + ' | DOI: ' + (it.get('doi') or ''))}</description>"
        )

        if it.get("abstract"):
            parts.append("<content:encoded><![CDATA[")
            parts.append(f"<p><strong>Journal</strong>: {it.get('journal','')}</p>")

            if it.get("authors"):
                parts.append(f"<p><strong>Authors</strong>: {'; '.join(it['authors'])}</p>")

            if it.get("doi"):
                parts.append(
                    f"<p><strong>DOI</strong>: "
                    f"<a href='https://doi.org/{it['doi']}'>{it['doi']}</a></p>"
                )

            parts.append("<hr/>")
            parts.append("<p><strong>Abstract</strong></p>")
            for block in it["abstract"].split("\n\n"):
                parts.append(f"<p>{block}</p>")

            parts.append("]]></content:encoded>")

        parts.append("</item>")

    parts.append("</channel></rss>")
    return "\n".join(parts)


# ---------------- Main ----------------

def main():
    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        items = json.load(f)

    open(PUBMED_DEBUG, "w").close()  # reset log
    enriched = []

    for it in items:
        it = dict(it)
        doi = extract_doi(it.get("doi") or it.get("link") or "")

        if doi:
            it["doi"] = doi
            cr = crossref_lookup(doi)
            time.sleep(RATE_LIMIT_SLEEP)

            if cr:
                it.setdefault("journal", (cr.get("container-title") or [None])[0])
                it.setdefault(
                    "authors",
                    [
                        " ".join(filter(None, [a.get("given"), a.get("family")]))
                        for a in cr.get("author", [])
                    ] or None,
                )
                it.setdefault("link", cr.get("URL"))

            pm = pubmed_fetch_by_doi(doi)
            time.sleep(RATE_LIMIT_SLEEP)

            if pm:
                it.setdefault("abstract", pm.get("abstract"))
                it.setdefault("authors", pm.get("authors"))
                it.setdefault("journal", pm.get("journal"))

        enriched.append(it)

    rss = build_rss(enriched)
    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(rss)


if __name__ == "__main__":
    main()
