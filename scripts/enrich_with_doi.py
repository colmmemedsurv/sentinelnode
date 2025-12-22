import json
import time
import re
import requests
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

INPUT_JSON = "data/relevant_items.json"
OUTPUT_RSS = "docs/betterdoi.xml"
PUBMED_LOG = "data/pubmed_raw_log.txt"

CROSSREF_API = "https://api.crossref.org/works"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

USER_AGENT = "sentinelnode/1.0 (mailto:colmmemedsurv@users.noreply.github.com)"
RATE_LIMIT_SLEEP = 0.3

DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)

Path("data").mkdir(exist_ok=True)


# ---------------- Utilities ----------------

def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )


def strip_xml(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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


def crossref_find_doi(title: str) -> str | None:
    try:
        r = requests.get(
            CROSSREF_API,
            params={"query.title": title, "rows": 3},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if r.status_code != 200:
            return None

        for item in r.json().get("message", {}).get("items", []):
            if item.get("DOI"):
                return item["DOI"]
    except Exception:
        pass

    return None


# ---------------- PubMed ----------------

def pubmed_abstract_from_doi(doi: str) -> str | None:
    try:
        # Step 1: DOI → PMID
        r = requests.get(
            PUBMED_SEARCH,
            params={
                "db": "pubmed",
                "term": doi,
                "retmode": "json",
            },
            timeout=20,
        )
        ids = r.json()["esearchresult"]["idlist"]
        if not ids:
            return None

        pmid = ids[0]

        # Step 2: PMID → Abstract
        r2 = requests.get(
            PUBMED_FETCH,
            params={
                "db": "pubmed",
                "id": pmid,
                "retmode": "xml",
            },
            timeout=20,
        )

        raw = r2.text

        with open(PUBMED_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n\n===== DOI {doi} =====\n")
            f.write(raw)

        m = re.search(r"<AbstractText[^>]*>(.*?)</AbstractText>", raw, re.S)
        if not m:
            return None

        return strip_xml(m.group(1))

    except Exception:
        return None


# ---------------- RSS builder ----------------

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
        "<description>Curated head &amp; neck cancer literature with DOI + PubMed enrichment.</description>",
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
            f"<description>{xml_escape(f'Journal: {it.get('journal','')} | DOI: {it.get('doi','')}')}</description>"
        )

        parts.append("<content:encoded><![CDATA[")
        parts.append(f"<p><strong>Journal</strong>: {it.get('journal','')}</p>")
        parts.append(f"<p><strong>Authors</strong>: {'; '.join(it.get('authors',[]))}</p>")
        if it.get("doi"):
            parts.append(
                f"<p><strong>DOI</strong>: <a href='https://doi.org/{it['doi']}'>{it['doi']}</a></p>"
            )
        parts.append("<hr/>")
        parts.append("<p><strong>Abstract</strong></p>")
        parts.append(f"<p>{it.get('abstract','Abstract not available')}</p>")
        parts.append("]]></content:encoded>")
        parts.append("</item>")

    parts.append("</channel></rss>")
    return "\n".join(parts)


# ---------------- Main ----------------

def main():
    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        items = json.load(f)

    open(PUBMED_LOG, "w").close()
    enriched = []

    for it in items:
        it = dict(it)

        doi = it.get("doi")
        if not doi and it.get("allow_doi_lookup"):
            doi = crossref_find_doi(it.get("title",""))
            time.sleep(RATE_LIMIT_SLEEP)

        it["doi"] = doi

        if doi:
            cr = crossref_lookup(doi)
            time.sleep(RATE_LIMIT_SLEEP)

            if cr:
                it["journal"] = cr.get("container-title",[None])[0]
                it["authors"] = [
                    " ".join(filter(None, [a.get("given"), a.get("family")]))
                    for a in cr.get("author", [])
                ]

            abstract = pubmed_abstract_from_doi(doi)
            it["abstract"] = abstract or "Abstract not available"

        enriched.append(it)

    rss = build_rss(enriched)
    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(rss)


if __name__ == "__main__":
    main()
