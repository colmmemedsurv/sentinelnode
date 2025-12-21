import json
import time
import requests
from datetime import datetime, timezone

INPUT_JSON = "data/relevant_items.json"
OUTPUT_RSS = "docs/betterdoi.xml"

CROSSREF_API = "https://api.crossref.org/works/"
USER_AGENT = "sentinelnode/1.0 (mailto:example@example.com)"
RATE_LIMIT_SLEEP = 0.2


def xml_escape(s):
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )


def lookup_doi(doi):
    try:
        r = requests.get(
            CROSSREF_API + doi,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json().get("message", {})
        return data
    except Exception:
        return None


def build_rss(items):
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<rss version="2.0">')
    parts.append("<channel>")
    parts.append("<title>Head &amp; Neck Cancer – DOI-Enriched Feed</title>")
    parts.append("<link>https://colmmemedsurv.github.io/sentinelnode/</link>")
    parts.append("<description>Curated head & neck cancer literature with DOI-based metadata enrichment when available.</description>")
    parts.append(f"<lastBuildDate>{xml_escape(now)}</lastBuildDate>")

    for it in items:
        parts.append("<item>")

        parts.append(f"<title>{xml_escape(it.get('title',''))}</title>")

        link = it.get("link") or ""
        if link:
            parts.append(f"<link>{xml_escape(link)}</link>")
            parts.append(f"<guid isPermaLink='true'>{xml_escape(link)}</guid>")
        else:
            parts.append(f"<guid isPermaLink='false'>{xml_escape(it.get('id',''))}</guid>")

        if it.get("published"):
            parts.append(f"<pubDate>{xml_escape(it['published'])}</pubDate>")

        description_bits = []

        description_bits.append(f"Journal: {it.get('journal','')}")
        description_bits.append(f"DOI: {it.get('doi_display','DOI not found')}")

        authors = it.get("authors") or []
        if authors:
            description_bits.append("Authors: " + ", ".join(authors))

        if it.get("abstract"):
            description_bits.append("\nAbstract:\n" + it["abstract"])

        parts.append(
            f"<description>{xml_escape(chr(10).join(description_bits))}</description>"
        )

        parts.append("</item>")

    parts.append("</channel>")
    parts.append("</rss>")

    return "\n".join(parts)


def main():
    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        items = json.load(f)

    enriched_items = []

    for it in items:
        doi = (it.get("doi") or "").strip()
        it = dict(it)  # copy

        if not doi:
            it["doi_display"] = "DOI not found"
            enriched_items.append(it)
            continue

        metadata = lookup_doi(doi)
        time.sleep(RATE_LIMIT_SLEEP)

        if metadata:
            # overwrite only if Crossref returns the field
            it["doi_display"] = doi

            if "container-title" in metadata and metadata["container-title"]:
                it["journal"] = metadata["container-title"][0]

            if "author" in metadata:
                authors = []
                for a in metadata["author"]:
                    name = " ".join(
                        part for part in [a.get("given"), a.get("family")] if part
                    )
                    if name:
                        authors.append(name)
                if authors:
                    it["authors"] = authors

            if "abstract" in metadata and metadata["abstract"]:
                it["abstract"] = metadata["abstract"]

            if "URL" in metadata and metadata["URL"]:
                it["link"] = metadata["URL"]

        else:
            it["doi_display"] = doi or "DOI not found"

        enriched_items.append(it)

    rss = build_rss(enriched_items)

    with open(OUTPUT_RSS, "w", encoding="utf-8") as f:
        f.write(rss)


if __name__ == "__main__":
    main()
