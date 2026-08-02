"""Institutional reports: finance, budget, enrolment and Facts & Figures.

UBC does not publish its financials or institutional research as an API -- they
are PDF/CSV/XLSX documents attached to pages on finance.ubc.ca and pair.ubc.ca
(Planning, Analytics & Institutional Research). So this collector builds an
*index*: every page, plus every document linked from it with a resolved URL,
file type and the page it came from. That index is the machine-readable part;
the documents themselves stay at their source URLs.
"""

from __future__ import annotations

import html as html_module
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from .base import Collector, Http, Output, register, wp_collection

SITES = {
    "finance": ("finance.ubc.ca", ("wp/v2/pages", "wp/v2/posts", "wp/v2/resources")),
    "pair": ("pair.ubc.ca", ("wp/v2/pages", "wp/v2/posts")),
    "facilities": ("facilities.ubc.ca", ("wp/v2/pages",)),
}

DOCUMENT_TYPES = {"pdf", "csv", "xlsx", "xls", "docx", "doc", "pptx", "zip"}
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("rendered", "")
    return value if isinstance(value, str) else ""


def _plain(value: str) -> str:
    return html_module.unescape(TAG_RE.sub(" ", value)).strip()


def _documents(
    record: dict[str, Any], base_url: str, site: str, body: str | None = None
) -> list[dict[str, Any]]:
    page_link = record.get("link") or base_url
    if body is None:
        body = _text(record.get("content")) + _text(record.get("excerpt"))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for href in HREF_RE.findall(body):
        url = urljoin(page_link, html_module.unescape(href))
        suffix = urlparse(url).path.rsplit(".", 1)[-1].lower() if "." in urlparse(url).path else ""
        if suffix not in DOCUMENT_TYPES or url in seen:
            continue
        seen.add(url)
        rows.append(
            {
                "site": site,
                "file_type": suffix,
                "url": url,
                "filename": urlparse(url).path.rsplit("/", 1)[-1],
                "page_title": _plain(_text(record.get("title"))),
                "page_url": page_link,
                "page_modified": record.get("modified_gmt") or record.get("modified"),
            }
        )
    return rows


@register
class Reports(Collector):
    name = "reports"
    title = "Institutional reports (finance, budget, enrolment, Facts & Figures)"
    description = (
        "An index of UBC's published institutional documents -- annual financial "
        "reports, budgets, tuition and fee schedules, annual enrolment reports and "
        "Facts & Figures sheets -- with a direct download URL, file type and the "
        "source page for each, plus the underlying page content."
    )
    sources = tuple(f"https://{host}" for host, _ in SITES.values())

    def collect(self, http: Http, out: Output) -> None:
        documents: list[dict[str, Any]] = []
        status: list[dict[str, Any]] = []

        for site, (host, paths) in SITES.items():
            for path in paths:
                source = f"https://{host}/wp-json/{path}"
                try:
                    records = wp_collection(http, host, path)
                except Exception as error:
                    status.append({"site": site, "endpoint": source, "records": 0,
                                   "skipped": f"{type(error).__name__}: {error}"})
                    continue

                collection = path.rsplit("/", 1)[-1]
                if records:
                    out.table(f"{site}/{collection}", records, source=source)

                empty = []
                for record in records:
                    found = _documents(record, f"https://{host}", site)
                    documents.extend(found)
                    # pair.ubc.ca returns every page with empty `content` over
                    # REST, so the PDFs are only visible in the rendered page.
                    if not found and not _text(record.get("content")).strip():
                        empty.append(record)

                recovered = self._from_html(http, empty, host, site)
                documents.extend(recovered)
                status.append({"site": site, "endpoint": source, "records": len(records),
                               "pages_refetched_as_html": len(empty),
                               "documents_recovered_from_html": len(recovered),
                               "skipped": None})

        # One page can link the same document twice; collapse on URL, but keep
        # how many pages referenced it -- a high count usually means boilerplate.
        counts: dict[str, int] = {}
        for row in documents:
            counts[row["url"]] = counts.get(row["url"], 0) + 1
        unique = list({row["url"]: row for row in documents}.values())
        for row in unique:
            row["linked_from_pages"] = counts[row["url"]]
        unique.sort(key=lambda row: (row["site"], row["file_type"], row["filename"]))
        out.describe(
            "documents",
            grain="one published document, deduplicated across the pages that link it",
            columns={
                "site": "finance, pair or facilities",
                "url": "direct download; the document itself is not mirrored here",
                "filename": "the file as UBC named it -- usually the only title it has",
                "file_type": "pdf, csv, xlsx...",
                "page_title": "the page it was linked from",
                "page_url": "that page",
                "linked_from_pages": "how many pages link it; a high count usually means boilerplate",
            },
            joins=[],
        )
        out.table("documents", unique)
        out.json("_sources.json", status)

    def _from_html(
        self, http: Http, records: list[dict[str, Any]], host: str, site: str
    ) -> list[dict[str, Any]]:
        """Re-read pages whose REST `content` came back empty.

        Some UBC sites (pair.ubc.ca in particular) build pages with blocks that
        the REST API does not render, so `content.rendered` is an empty string
        even though the published page links the reports we are after.
        """
        if not records:
            return []

        def scrape(record: dict[str, Any]) -> list[dict[str, Any]]:
            link = record.get("link")
            if not link:
                return []
            try:
                body = http.get(link).text
            except Exception:
                return []
            return _documents(record, f"https://{host}", site, body=body)

        return [row for chunk in http.map(scrape, records) for row in chunk]
