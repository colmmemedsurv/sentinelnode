import json
import time
import re
from datetime import datetime, timezone
from email.utils import format_datetime
import requests
import xml.etree.ElementTree as ET

INPUT_JSON = "data/relevant_items.json"
OUTPUT_RSS = "docs/betterdoi.xml"
PUBMED_LOG = "docs/pubmed_debug.txt"

CROSSREF_API = "https://api.crossref.org/works"
PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

USER_AGENT = "sentinelnode/1.0 (mailto:colmmemedsurv@users.noreply.github.com)"
RATE_LIMIT_SLEEP = 0.3

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

def pubmed_lookup_abstract(doi: str, logf) -> str | None:
    """
    DOI -> PMID -> Abstract
    """
    try:
        # DOI → PMID
        r = requests.get(
            PUBMED_ESEARCH,
            params={
                "db": "pubmed",
                "term": f"{doi}[DOI]",
                "retmode": "xml",
            },
            timeout=20,
        )
        logf.write(f"\n=== ESEARCH DOI: {doi} ===\n{r.text}\n")

        root = ET.fromstring(r.text)
        ids = root.findall(".//Id")
        if not ids:
            return None

        pmid = ids[0].text

        # PMID → Abstract
        r2 = requests.get(
            PUBMED_EFETCH,
            params={
                "db": "pubmed",
                "id": pmid,
                "retmode": "xml",
            },
            timeout=20,
        )
        logf.write(f"\n=== EFETCH PMID: {pmid} ===\n{r2.text}\n")

        root2 = ET.fromstring(r2.text)
        abs_nodes = root2.findall(".//AbstractText")

        if not abs_nodes:
            return None

        abstract = " ".join(
            strip_xml_tags(a.text or "") for a in abs_nodes
        ).strip()

        return abstract if abstract else None

    except Exception as e:
        logf.write(f"\nERROR for DOI {doi}: {e}\n")
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
        "<description>Curated head &amp; neck cancer literature with DOI, Crossref and PubMed enrichment.</description>",
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

        if it.get("doi"):
            parts.append(f"<prism:doi>{xml_escape(it['doi'])}</prism:doi>")

        desc = f"Journal: {it.get('journal','')} | DOI: {it.get('doi','')}"
        parts.append(f"<description>{xml_escape(desc)}</description>")

        parts.append("<content:encoded><![CDATA[")

        parts.append(f"<p><strong>Journal</strong>: {it.get('journal','')}</p>")

        if it.get("authors"):
            parts.append(f"<p><strong>Authors</strong>: {'; '.join(it['authors'])}</p>")

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

    enriched = []

    with open(PUBMED_LOG, "w", encoding="utf-8") as logf:
        for it in items:
            it = dict(it)
            doi = (it.get("doi") or "").strip()
            it["doi"] = doi

            cr = crossref_lookup(doi) if doi else None
            time.sleep(RATE_LIMIT_SLEEP)

            if cr:
                it["journal"] = it.get("journal") or (cr.get("container-title") or [""])[0]
                it["authors"] = it.get("authors") or [
                    " ".join(filter(None, [a.get("given"), a.get("family")]))
                    for a in cr.get("author", [])
                ]

                if not it.get("pubDate"):
                    dp = cr.get("issued", {}).get("date-parts")
                    if dp:
                        y, m, d = dp[0] + [1, 1]
                        it["pubDate"] = format_datetime(datetime(y, m, d, tzinfo=timezone.utc))

            if doi:
                abstract = pubmed_lookup_abstract(doi, logf)
                time.sleep(RATE_LIMIT_SLEEP)
                it["abstract"] = abstract or "Abstract not available"

            enriched.append(it)

    rss = build_rss(enriched)
    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(rss)


if __name__ == "__main__":
    main()
