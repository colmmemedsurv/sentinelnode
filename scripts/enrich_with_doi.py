import json
import time
import re
import os
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime

INPUT_JSON = "data/relevant_items.json"
OUTPUT_RSS = "docs/betterdoi.xml"
PUBMED_DEBUG = "data/pubmed_raw_debug.txt"

CROSSREF_API = "https://api.crossref.org/works"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

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


# ---------------- Crossref ----------------

def crossref_lookup(doi: str) -> dict | None:
    try:
        r = requests.get(
            f"{CROSSREF_API}/{doi}",
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
    """Returns dict with abstract, authors, journal, pubdate"""
    try:
        s = requests.get(
            PUBMED_SEARCH,
            params={
                "db": "pubmed",
                "term": f"{doi}[DOI]",
                "retmode": "json",
            },
            timeout=20,
        )
        ids = s.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return None

        f = requests.get(
            PUBMED_FETCH,
            params={
                "db": "pubmed",
                "id": ids[0],
                "retmode": "xml",
            },
            timeout=20,
        )

        raw_xml = f.text
        with open(PUBMED_DEBUG, "a", encoding="utf-8") as dbg:
            dbg.write(f"\n===== DOI {doi} =====\n")
            dbg.write(raw_xml)
            dbg.write("\n\n")

        root = ET.fromstring(raw_xml)

        # ----- Abstract (FULL, all sections)
        abstract_parts = []
        for node in root.findall(".//AbstractText"):
            label = node.attrib.get("Label")
            txt = "".join(node.itertext()).strip()
            if label:
                abstract_parts.append(f"{label}: {txt}")
            else:
                abstract_parts.append(txt)

        abstract = "\n\n".join(abstract_parts)

        # ----- Authors
        authors = []
        for a in root.findall(".//Author"):
            last = a.findtext("LastName")
            fore = a.findtext("ForeName")
            if last and fore:
                authors.append(f"{fore} {last}")

        # ----- Journal
        journal = root.findtext(".//Journal/Title")

        # ----- Pub date
        y = root.findtext(".//PubDate/Year")
        m = root.findtext(".//PubDate/Month") or "01"
        d = root.findtext(".//PubDate/Day") or "01"
        try:
            pubdate = format_datetime(
                datetime(int(y), int(m), int(d), tzinfo=timezone.utc)
            )
        except Exception:
            pubdate = None

        return {
            "abstract": abstract,
            "authors": authors,
            "journal": journal,
            "pubDate": pubdate,
        }

    except Exception:
        return None


# ---------------- RSS ----------------

def build_rss(items: list[dict]) -> str:
    now = format_datetime(datetime.now(timezone.utc))
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">',
        "<channel>",
        "<title>Head &amp; Neck Cancer – DOI-Enriched Feed</title>",
        "<link>https://colmmemedsurv.github.io/sentinelnode/</link>",
        "<description>Curated head &amp; neck cancer literature with DOI, Crossref and PubMed enrichment.</description>",
        f"<lastBuildDate>{xml_escape(now)}</lastBuildDate>",
    ]

    for it in items:
        out.append("<item>")
        out.append(f"<title>{xml_escape(it.get('title',''))}</title>")

        if it.get("link"):
            out.append(f"<link>{xml_escape(it['link'])}</link>")
            out.append(f"<guid isPermaLink='true'>{xml_escape(it['link'])}</guid>")

        if it.get("pubDate"):
            out.append(f"<pubDate>{xml_escape(it['pubDate'])}</pubDate>")

        if it.get("authors"):
            out.append(f"<dc:creator>{xml_escape('; '.join(it['authors']))}</dc:creator>")

        if it.get("journal"):
            out.append(f"<prism:publicationName>{xml_escape(it['journal'])}</prism:publicationName>")

        if it.get("doi"):
            out.append(f"<prism:doi>{xml_escape(it['doi'])}</prism:doi>")

        desc = f"Journal: {it.get('journal','')} | DOI: {it.get('doi','')}"
        out.append(f"<description>{xml_escape(desc)}</description>")

        if it.get("abstract"):
            out.append("<content:encoded><![CDATA[")
            out.append(f"<p><strong>Journal</strong>: {it.get('journal','')}</p>")
            if it.get("authors"):
                out.append(f"<p><strong>Authors</strong>: {'; '.join(it['authors'])}</p>")
            if it.get("doi"):
                out.append(f"<p><strong>DOI</strong>: <a href='https://doi.org/{it['doi']}'>{it['doi']}</a></p>")
            out.append("<hr/>")
            out.append("<p><strong>Abstract</strong></p>")
            out.append(f"<p>{xml_escape(it['abstract'])}</p>")
            out.append("]]></content:encoded>")

        out.append("</item>")

    out.append("</channel></rss>")
    return "\n".join(out)


# ---------------- Main ----------------

def main():
    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        items = json.load(f)

    open(PUBMED_DEBUG, "w").close()
    enriched = []

    for it in items:
        it = dict(it)
        doi = extract_doi(it.get("doi") or it.get("link"))

        it["doi"] = doi

        # --- PubMed only if DOI exists AND feed allowed
        if doi and it.get("allow_doi_lookup"):
            pm = pubmed_fetch_by_doi(doi)
            time.sleep(RATE_LIMIT_SLEEP)

            if pm:
                it.setdefault("abstract", pm.get("abstract"))
                it.setdefault("authors", pm.get("authors"))
                it.setdefault("journal", pm.get("journal"))
                it.setdefault("pubDate", pm.get("pubDate"))

        enriched.append(it)

    rss = build_rss(enriched)
    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(rss)


if __name__ == "__main__":
    main()
