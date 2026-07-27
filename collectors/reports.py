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


def _documents(record: dict[str, Any], base_url: str, site: str) -> list[dict[str, Any]]:
    page_link = record.get("link") or base_url
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
                for record in records:
                    documents.extend(_documents(record, f"https://{host}", site))
                status.append({"site": site, "endpoint": source, "records": len(records),
                               "skipped": None})

        # One page can link the same PDF twice; collapse on URL.
        unique = list({row["url"]: row for row in documents}.values())
        unique.sort(key=lambda row: (row["site"], row["file_type"], row["filename"]))
        out.table("documents", unique)
        out.json("_sources.json", status)
