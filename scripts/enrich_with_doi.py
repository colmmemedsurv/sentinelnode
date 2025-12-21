import json
import time
import re
from datetime import datetime, timezone
from email.utils import format_datetime

import requests
from openai import OpenAI

INPUT_JSON = "data/relevant_items.json"
OUTPUT_RSS = "docs/betterdoi.xml"

CROSSREF_API = "https://api.crossref.org/works"
USER_AGENT = "sentinelnode/1.0 (mailto:colmmemedsurv@users.noreply.github.com)"
RATE_LIMIT_SLEEP = 0.25

MODEL = "gpt-4.1-mini"  # comparator only

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
    text = re.sub(r"\b[jJ]ats:[A-Za-z0-9_-]+\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("-", " ")
    s = re.sub(r"[^\w\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def extract_doi(s: str | None) -> str | None:
    if not s:
        return None
    m = DOI_RE.search(str(s))
    return m.group(0) if m else None


# ---------------- Crossref ----------------

def crossref_lookup_doi(doi: str) -> dict | None:
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


def crossref_search(title: str, rows=5) -> list[dict]:
    try:
        r = requests.get(
            CROSSREF_API,
            params={"query.title": title, "rows": rows},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if r.status_code != 200:
            return []
        return r.json().get("message", {}).get("items", [])
    except Exception:
        return []


# ---------------- OpenAI comparator ----------------

def openai_same_article(
    client: OpenAI,
    rss_title: str,
    rss_authors: list[str],
    cr_title: str,
    cr_authors: list[str],
) -> bool:
    prompt = f"""
You are deciding whether TWO citations refer to the SAME journal article.

Answer ONLY YES or NO.

Citation A:
Title: {rss_title}
Authors: {", ".join(rss_authors)}

Citation B:
Title: {cr_title}
Authors: {", ".join(cr_authors)}
"""

    resp = client.responses.create(
        model=MODEL,
        input=prompt,
    )
    out = (resp.output_text or "").strip().upper()
    return out == "YES"


# ---------------- Matching logic ----------------

def author_overlap(a: list[str], b: list[str]) -> bool:
    a_set = {x.lower() for x in a if x}
    b_set = {x.lower() for x in b if x}
    return bool(a_set & b_set)


def recover_doi(item: dict, client: OpenAI) -> str | None:
    rss_title = item.get("title") or ""
    rss_authors = item.get("authors") or []

    wanted = norm_title(rss_title)
    candidates = crossref_search(rss_title)

    for cand in candidates:
        cr_title = (cand.get("title") or [""])[0]
        if norm_title(cr_title) == wanted:
            return cand.get("DOI")

    for cand in candidates:
        cr_title = (cand.get("title") or [""])[0]
        if wanted in norm_title(cr_title) or norm_title(cr_title) in wanted:
            if author_overlap(
                rss_authors,
                [
                    " ".join(filter(None, [a.get("given"), a.get("family")]))
                    for a in cand.get("author", [])
                ],
            ):
                return cand.get("DOI")

    for cand in candidates:
        cr_title = (cand.get("title") or [""])[0]
        cr_authors = [
            " ".join(filter(None, [a.get("given"), a.get("family")]))
            for a in cand.get("author", [])
        ]

        if openai_same_article(
            client,
            rss_title,
            rss_authors,
            cr_title,
            cr_authors,
        ):
            return cand.get("DOI")

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
        "<description>Curated head &amp; neck cancer literature with conservative DOI recovery.</description>",
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

        if it.get("doi_display") and it["doi_display"] != "DOI not found":
            parts.append(f"<prism:doi>{xml_escape(it['doi_display'])}</prism:doi>")

        parts.append(
            f"<description>{xml_escape(f'Journal: {it.get('journal','')} | DOI: {it.get('doi_display','')}')}</description>"
        )

        if it.get("abstract"):
            parts.append("<content:encoded><![CDATA[")
            parts.append(f"<p><strong>Journal</strong>: {it.get('journal','')}</p>")
            if it.get("authors"):
                parts.append(
                    f"<p><strong>Authors</strong>: {'; '.join(it['authors'])}</p>"
                )
            if it.get("doi_display") != "DOI not found":
                parts.append(
                    f"<p><strong>DOI</strong>: <a href='https://doi.org/{it['doi_display']}'>{it['doi_display']}</a></p>"
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

    client = OpenAI()
    enriched = []

    for it in items:
        it = dict(it)
        doi = (it.get("doi") or "").strip()

        if not doi and it.get("allow_doi_lookup"):
            doi = recover_doi(it, client)
            time.sleep(RATE_LIMIT_SLEEP)

        it["doi_display"] = doi if doi else "DOI not found"

        cr = crossref_lookup_doi(doi) if doi else None
        if cr:
            time.sleep(RATE_LIMIT_SLEEP)

            if not it.get("journal") and cr.get("container-title"):
                it["journal"] = cr["container-title"][0]

            if not it.get("authors") and cr.get("author"):
                it["authors"] = [
                    " ".join(filter(None, [a.get("given"), a.get("family")]))
                    for a in cr["author"]
                ]

            if not it.get("abstract") and cr.get("abstract"):
                it["abstract"] = strip_xml_tags(cr["abstract"])

            if not it.get("link"):
                it["link"] = cr.get("URL") or f"https://doi.org/{doi}"

            pub = cr.get("issued", {}).get("date-parts")
            if pub:
                y, m, d = pub[0] + [1, 1]
                dt = datetime(y, m, d, tzinfo=timezone.utc)
                it["pubDate"] = format_datetime(dt)

        if it.get("abstract"):
            it["abstract"] = strip_xml_tags(it["abstract"])

        enriched.append(it)

    rss = build_rss(enriched)
    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(rss)


if __name__ == "__main__":
    main()
