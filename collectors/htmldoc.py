"""Minimal HTML reading for the few UBC pages that aren't served as data.

Nearly everything here arrives as JSON. A handful of things don't: the Academic
Calendar's dates-and-deadlines and fee tables, HR's statutory holiday tables,
the heading outline inside calendar page bodies, and the admission-requirement
fragments you.ubc.ca returns over AJAX. All of them are headings, tables and
lists, which `html.parser` from the standard library handles fine -- so
`requests` stays the only dependency this project has.

Two readers, because the pages come in two shapes:

- `blocks()` returns headings and tables *interleaved in document order*, because
  both date pages use the heading above a table as its context: a `<h4>` month on
  the HR page, a `<h3>` session on the calendar page. Read them as a stream and
  the context is just whatever heading you saw last.
- `sections()` returns headings with the prose and list items that follow them,
  because that is how UBC writes admission requirements -- an `<h4>` naming the
  rule and a `<ul>` of the courses it demands. `text()` and `items()` are the
  same reader over a fragment with no headings in it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

# Content that is markup machinery rather than text.
IGNORED_TAGS = {"script", "style", "noscript"}

# Tags where the surrounding text should not run together: "<td>a</td><td>b</td>"
# is "a" and "b", never "ab".
BREAK_TAGS = {"br", "p", "div", "li", "tr", "td", "th", "hr"}


# Non-breaking space, soft hyphen, zero-width space -- the invisible characters
# UBC's CMS sprinkles through page text. Given by codepoint rather than typed
# literally, so none of them can sit unnoticed in this file.
INVISIBLE = {0xA0: " ", 0xAD: "", 0x200B: ""}


def clean(text: str) -> str:
    """Collapse whitespace and drop the invisible characters UBC's CMS emits."""
    return re.sub(r"\s+", " ", (text or "").translate(INVISIBLE)).strip()


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Table:
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


class _Reader(HTMLParser):
    """Collects headings and tables; ignores everything else on the page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Heading | Table] = []
        self._ignore = 0
        self._heading: tuple[int, list[str]] | None = None
        self._tables: list[Table] = []
        self._row: list[str] | None = None
        self._row_is_header = False
        self._cell: list[str] | None = None

    # -- collection ---------------------------------------------------------

    def handle_data(self, data: str) -> None:
        if self._ignore:
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._heading is not None:
            self._heading[1].append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in IGNORED_TAGS:
            self._ignore += 1
        elif tag == "table":
            # A table nested inside a cell ends that cell's text run.
            self._close_cell()
            self._tables.append(Table())
        elif tag == "tr" and self._tables:
            self._close_row()
            self._row, self._row_is_header = [], False
        elif tag in ("td", "th") and self._tables:
            self._close_cell()
            if self._row is None:
                self._row, self._row_is_header = [], False
            self._cell = []
            self._row_is_header = self._row_is_header or tag == "th"
        elif tag in HEADING_TAGS and not self._tables:
            self._close_heading()
            self._heading = (HEADING_TAGS[tag], [])
        elif tag in BREAK_TAGS:
            if self._cell is not None:
                self._cell.append(" ")
            elif self._heading is not None:
                self._heading[1].append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in IGNORED_TAGS:
            self._ignore = max(0, self._ignore - 1)
        elif tag == "table":
            self._close_table()
        elif tag == "tr":
            self._close_row()
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag in HEADING_TAGS:
            self._close_heading()

    # -- flushing -----------------------------------------------------------

    def _close_cell(self) -> None:
        if self._cell is None:
            return
        text = clean("".join(self._cell))
        self._cell = None
        if self._row is not None:
            self._row.append(text)

    def _close_row(self) -> None:
        self._close_cell()
        row, self._row = self._row, None
        is_header, self._row_is_header = self._row_is_header, False
        if not row or not any(row) or not self._tables:
            return
        table = self._tables[-1]
        # A leading all-<th> row is the header, whether or not it sits in a
        # <thead>; anything after the first row is data even if it uses <th>.
        if is_header and not table.headers and not table.rows:
            table.headers = row
        else:
            table.rows.append(row)

    def _close_table(self) -> None:
        self._close_row()
        if not self._tables:
            return
        table = self._tables.pop()
        if table.headers or table.rows:
            self.blocks.append(table)

    def _close_heading(self) -> None:
        if self._heading is None:
            return
        level, chunks = self._heading
        self._heading = None
        text = clean("".join(chunks))
        if text:
            self.blocks.append(Heading(level, text))

    def result(self) -> list[Heading | Table]:
        """Close anything the document left open, then hand back the blocks."""
        self._close_heading()
        while self._tables:
            self._close_table()
        return self.blocks


def blocks(html: str) -> list[Heading | Table]:
    """Every heading and table on the page, in document order."""
    reader = _Reader()
    reader.feed(html or "")
    reader.close()
    return reader.result()


def tables(html: str) -> list[Table]:
    return [block for block in blocks(html) if isinstance(block, Table)]


def headings(html: str) -> list[Heading]:
    return [block for block in blocks(html) if isinstance(block, Heading)]


# --------------------------------------------------------------------------
# Prose: headings with the text and list items that follow them
# --------------------------------------------------------------------------


@dataclass
class Section:
    """A heading and everything written under it, up to the next heading."""

    level: int
    heading: str
    text: str = ""
    items: list[str] = field(default_factory=list)


class _Prose(HTMLParser):
    """Splits a fragment into sections, keeping each `<li>` as its own string.

    A requirement list is the unit that matters -- "Pre-Calculus 12" is one
    requirement whether UBC wrote it in a two-item list or a ten-item one -- so
    list items are collected individually rather than flattened into a run of
    prose. Content before the first heading lands in a section with an empty
    heading, which is the normal case for the AJAX fragments.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[Section] = [Section(level=0, heading="")]
        self._ignore = 0
        self._sink: list[str] | None = None
        self._is_heading = False
        self._is_item = False
        self._pending_level = 0
        self._prose: list[str] = []

    # -- collection ---------------------------------------------------------

    def handle_data(self, data: str) -> None:
        if self._ignore:
            return
        (self._sink if self._sink is not None else self._prose).append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in IGNORED_TAGS:
            self._ignore += 1
        elif tag in HEADING_TAGS:
            self._flush()
            self._sink, self._is_heading = [], True
            self._pending_level = HEADING_TAGS[tag]
        elif tag == "li":
            self._flush()
            self._sink, self._is_item = [], True
        elif tag in BREAK_TAGS:
            (self._sink if self._sink is not None else self._prose).append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in IGNORED_TAGS:
            self._ignore = max(0, self._ignore - 1)
        elif tag in HEADING_TAGS or tag == "li":
            self._flush()

    # -- flushing -----------------------------------------------------------

    def _flush(self) -> None:
        """Close whatever run of text is open and file it in the right place."""
        if self._sink is None:
            return
        content = clean("".join(self._sink))
        is_heading, is_item = self._is_heading, self._is_item
        self._sink, self._is_heading, self._is_item = None, False, False

        if is_heading:
            # Prose sitting between the previous heading and this one belongs to
            # the previous heading, so bank it before opening the new section.
            self._bank_prose()
            if content:
                self.sections.append(Section(level=self._pending_level, heading=content))
        elif is_item and content:
            self._bank_prose()
            self.sections[-1].items.append(content)

    def _bank_prose(self) -> None:
        text = clean("".join(self._prose))
        self._prose = []
        if not text:
            return
        current = self.sections[-1]
        current.text = f"{current.text} {text}".strip() if current.text else text

    def result(self) -> list[Section]:
        self._flush()
        self._bank_prose()
        return [s for s in self.sections if s.heading or s.text or s.items]


def sections(html: str) -> list[Section]:
    """Every heading in a fragment with the prose and list items beneath it."""
    reader = _Prose()
    reader.feed(html or "")
    reader.close()
    return reader.result()


def items(html: str) -> list[str]:
    """The list items in a fragment, in order.

    Falls back to the fragment's prose when UBC writes a requirement as a
    sentence instead of a list, so a caller always gets the content either way.
    """
    found = [item for section in sections(html) for item in section.items]
    if found:
        return found
    prose = text(html)
    return [prose] if prose else []


def text(html: str) -> str:
    """A fragment as plain text, list items included, whitespace collapsed."""
    parts: list[str] = []
    for section in sections(html):
        parts.extend(filter(None, [section.heading, section.text, *section.items]))
    return clean(" ".join(parts))
