import json
import time
import re
import os
from datetime import datetime, timezone
from email.utils import format_datetime

import requests

# ---------------- Config ----------------

INPUT_JSON = "data/relevant_items.json"
OUTPUT_RSS = "docs/betterdoi.xml"

CROSSREF_API = "https://api.crossref.org/works"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

USER_AGENT = "sentinelnode/1.0 (mailto:colmmemedsurv@users.noreply.github.com)"
RATE_LIMIT_SLEEP = 0.25

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
    text = re.sub(r"\s+", " ", text).strip()
    return text

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

def pubmed_abstract_by_doi(doi: str) -> str | None:
    """
    DOI -> PMID -> Abstract
    Returns abstract text if PubMed explicitly provides one.
    """
    try:
        r = requests.get(
            PUBMED_SEARCH,
            params={
                "db": "pubmed",
                "term": f"{doi}[DOI]",
                "retmode": "json",
            },
            timeout=20,
        )
        if r.status_code != 200:
            return None

        ids = r.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return None

        pmid = ids[0]

        r = requests.get(
            PUBMED_FETCH,
            params={
                "db": "pubmed",
                "id": pmid,
                "retmode": "xml",
            },
            timeout=20,
        )
        if r.status_code != 200:
            return None

        xml = r.text
        m = re.search(r"<AbstractText[^>]*>(.*?)</AbstractText>", xml, re.S)
        if not m:
            return None

        return strip_xml_tags(m.group(1))

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
        "<description>Curated head &amp; neck cancer literature with DOI and PubMed enrichment.</description>",
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
                f"<dc:creator>{xml_escape('; '.join(it['authors']))}</dc:creator>"
            )

        if it.get("journal"):
            parts.append(
                f"<prism:publicationName>{xml_escape(it['journal'])}</prism:publicationName>"
            )

        parts.append(f"<prism:doi>{xml_escape(it['doi_display'])}</prism:doi>")

        parts.append(
            f"<description>{xml_escape(f'Journal: {it.get('journal','')} | DOI: {it.get('doi_display','')}')}</description>"
        )

        parts.append("<content:encoded><![CDATA[")

        parts.append(f"<p><strong>Journal</strong>: {it.get('journal','')}</p>")

        if it.get("authors"):
            parts.append(f"<p><strong>Authors</strong>: {'; '.join(it['authors'])}</p>")

        doi_url = f"https://doi.org/{it['doi_display']}"
        parts.append(
            f"<p><strong>DOI</strong>: <a href='{doi_url}'>{it['doi_display']}</a></p>"
        )

        parts.append("<hr/>")
        parts.append("<p><strong>Abstract</strong></p>")
        parts.append(f"<p>{it['abstract']}</p>")

        parts.append("]]></content:encoded>")
        parts.append("</item>")

    parts.append("</channel></rss>")
    return "\n".join(parts)

# ---------------- Main ----------------

def main():
    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        items = json.load(f)

    enriched = []

    for it in items:
        it = dict(it)
        doi = (it.get("doi") or "").strip()
        it["doi_display"] = doi if doi else "DOI not found"

        cr = crossref_lookup(doi) if doi else None
        if cr:
            time.sleep(RATE_LIMIT_SLEEP)

            if cr.get("container-title"):
                it["journal"] = cr["container-title"][0]

            if cr.get("author"):
                it["authors"] = [
                    " ".join(filter(None, [a.get("given"), a.get("family")]))
                    for a in cr["author"]
                ]

            if cr.get("issued", {}).get("date-parts"):
                y, m, d = cr["issued"]["date-parts"][0] + [1, 1]
                dt = datetime(y, m, d, tzinfo=timezone.utc)
                it["pubDate"] = format_datetime(dt)

            if not it.get("link"):
                it["link"] = cr.get("URL") or f"https://doi.org/{doi}"

        # ---- PubMed abstract (DOI-based only) ----
        abstract = it.get("abstract")
        if not abstract or abstract.lower().startswith("publication date"):
            pm_abs = pubmed_abstract_by_doi(doi) if doi else None
            if pm_abs:
                it["abstract"] = pm_abs
            else:
                it["abstract"] = "Abstract not available (not provided by RSS or PubMed)."

        enriched.append(it)

    rss = build_rss(enriched)
    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(rss)

if __name__ == "__main__":
    main()
