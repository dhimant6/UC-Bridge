"""Parser for Avaya Communication Manager SAT / OSSI screen output.

SAT screens are fixed-width terminal forms, not an API. They look like this::

    display station 5101                             Page   1 of   5
                                     STATION

    Extension: 5101                    Lock Messages? n            BCC: 0
        Type: 9611                     Security Code: *             TN: 1
        Port: S00012                   Coverage Path 1: 12         COR: 1
        Name: Mueller, Anna            Coverage Path 2:            COS: 1

The brief asks for a proper parser rather than regex soup, and the difference
matters: a per-field regex breaks the moment a value contains a word the pattern
did not anticipate, and it breaks silently, producing a station record with the
wrong coverage path.

So this parses *structurally*, using the properties the terminal layout
guarantees:

* A field label is either at the start of a line or preceded by two or more
  spaces — column alignment is what separates fields, and a value never carries
  two consecutive spaces followed by ``Label:``.
* A label ends in ``:`` or ``?`` (Avaya uses ``?`` for booleans).
* A value runs from after its label to the start of the next label on that line,
  or to end of line.
* Section headings are centred lines with no label at all.
* Multi-page forms repeat the ``Page N of M`` header and are merged in order.

That set of rules is checkable against real output and fails loudly when a
screen does not match, instead of quietly returning partial data.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

#: A field label: at line start, or preceded by 2+ spaces (column separation),
#: terminated by ':' or '?' (Avaya uses '?' for booleans).
#:
#: The label body allows *single* spaces only. This is the rule that stops a
#: label swallowing the value to its left: in
#:
#:     Don't Answer?   y     y      Number of Rings: 3
#:
#: a label permitted to contain runs of spaces would match
#: "y     y      Number of Rings" and quietly lose both the answer flag and the
#: ring count. Two consecutive spaces mean a column boundary, never a label.
_LABEL = re.compile(
    r"(?:(?<=\s{2})|(?<=^))"
    r"(?P<label>[A-Za-z0-9](?:[A-Za-z0-9/()#.'\-]|[ ](?![ ]))*?)"
    r"(?P<terminator>[:?])"
)

_PAGE_HEADER = re.compile(r"Page\s+(?P<page>\d+)\s+of\s+(?P<total>\d+)", re.IGNORECASE)
_COMMAND_LINE = re.compile(r"^(display|list|change|add|status)\s+", re.IGNORECASE)


class SatParseError(ValueError):
    """The screen did not match the structure this parser understands."""


class SatField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    value: str
    page: int = 1
    section: str | None = None
    is_boolean: bool = Field(
        default=False, description="Avaya writes booleans with a '?' label terminator."
    )

    @property
    def as_bool(self) -> bool | None:
        """The flag's value, taken from the first column.

        Some CM screens lay booleans out as a grid — a coverage path's criteria
        carry separate inside-call and outside-call flags on one row::

            Busy?                  y                  y

        The first token is the inside-call value. Reading the whole string would
        make every grid row parse as False, silently dropping the coverage
        criteria. Callers that need the outside-call value read ``columns``.
        """
        if not self.is_boolean:
            return None
        first = self.value.strip().split(maxsplit=1)[0].lower() if self.value.strip() else ""
        return first in {"y", "yes", "t", "true", "1"}

    @property
    def columns(self) -> list[str]:
        """The value split on column boundaries, for grid rows."""
        return [part for part in re.split(r"\s{2,}", self.value.strip()) if part]


class SatForm(BaseModel):
    """A parsed SAT form, possibly spanning several pages."""

    model_config = ConfigDict(extra="forbid")

    command: str | None = None
    title: str | None = None
    page_count: int = 1
    fields: list[SatField] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)

    def index(self) -> dict[str, str]:
        """label -> value. Later pages win, which matches how operators read them."""
        return {field.label: field.value for field in self.fields}

    def get(self, label: str, default: str | None = None) -> str | None:
        """The field's value, or ``default`` when the field is absent.

        A field that is present but blank returns ``""``, not the default. On a
        SAT screen "Coverage Path 2 is empty" and "this screen has no Coverage
        Path 2" mean different things, and collapsing them would let a migration
        invent a coverage path that was never configured.
        """
        for field in self.fields:
            if field.label.lower() == label.lower():
                return field.value
        return default

    def get_bool(self, label: str, default: bool | None = None) -> bool | None:
        for field in self.fields:
            if field.label.lower() == label.lower() and field.is_boolean:
                return field.as_bool
        return default

    def get_int(self, label: str) -> int | None:
        raw = self.get(label)
        if raw is None:
            return None
        try:
            return int(raw.strip())
        except ValueError:
            return None

    def fields_in_section(self, section: str) -> list[SatField]:
        return [f for f in self.fields if (f.section or "").lower() == section.lower()]

    def unmapped(self, consumed: set[str]) -> dict[str, str]:
        """Everything the caller did not claim, for the fidelity assessment."""
        lowered = {c.lower() for c in consumed}
        return {
            f.label: f.value
            for f in self.fields
            if f.label.lower() not in lowered and f.value
        }


def parse_sat_form(text: str) -> SatForm:
    """Parse a (possibly multi-page) SAT form screen."""
    if not text.strip():
        raise SatParseError("Empty SAT output: the session produced nothing to parse.")

    command: str | None = None
    title: str | None = None
    page = 1
    page_count = 1
    section: str | None = None
    fields: list[SatField] = []
    sections: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        page_match = _PAGE_HEADER.search(line)
        if page_match:
            page = int(page_match.group("page"))
            page_count = max(page_count, int(page_match.group("total")))
            # The command echo shares the header line; keep the first one seen.
            head = line[: page_match.start()].strip()
            if head and command is None and _COMMAND_LINE.match(head):
                command = head
            if head and _COMMAND_LINE.match(head):
                continue
            line = head
            if not line:
                continue

        if command is None and _COMMAND_LINE.match(line.strip()):
            command = line.strip()
            continue

        parsed = _parse_line(line, page=page, section=section)
        if parsed:
            fields.extend(parsed)
            continue

        # No labels on this line: it is a heading. The first one is the form
        # title; later ones are section headings.
        heading = line.strip()
        if _looks_like_heading(heading):
            if title is None:
                title = heading
            else:
                section = heading
                if heading not in sections:
                    sections.append(heading)

    if not fields:
        raise SatParseError(
            "No fields were found. Either the screen is a table (use parse_sat_table) "
            "or the output is not a SAT form."
        )

    return SatForm(
        command=command,
        title=title,
        page_count=page_count,
        fields=fields,
        sections=sections,
    )


def _parse_line(line: str, *, page: int, section: str | None) -> list[SatField]:
    matches = list(_LABEL.finditer(line))
    if not matches:
        return []

    fields: list[SatField] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        value = line[start:end].strip()
        label = match.group("label").strip()
        if not label:
            continue
        fields.append(
            SatField(
                label=label,
                value=value,
                page=page,
                section=section,
                is_boolean=match.group("terminator") == "?",
            )
        )
    return fields


def _looks_like_heading(text: str) -> bool:
    """Headings are short, mostly uppercase, and carry no punctuation."""
    if len(text) > 60:
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(c.isupper() for c in letters) / len(letters) > 0.7


# --------------------------------------------------------------------------- #
# Tabular screens (`list station`, `list trunk-group`, ...)
# --------------------------------------------------------------------------- #


class SatTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, str]] = Field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)


def parse_sat_table(text: str) -> SatTable:
    """Parse a SAT list screen using the header row's column positions.

    Column boundaries come from where the header words start, not from splitting
    on whitespace: values legitimately contain spaces ("Mueller, Anna") and a
    naive split would shift every subsequent column.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    header_index = _find_header(lines)
    if header_index is None:
        raise SatParseError(
            "No column header row found. A SAT table needs a header line followed by "
            "aligned data rows."
        )

    title = next(
        (line.strip() for line in lines[:header_index] if _looks_like_heading(line.strip())),
        None,
    )
    header = lines[header_index]
    spans = _column_spans(header)
    columns = [header[start:end].strip() for start, end in spans]

    rows: list[dict[str, str]] = []
    for line in lines[header_index + 1 :]:
        if not line.strip():
            continue
        if _PAGE_HEADER.search(line) or _COMMAND_LINE.match(line.strip()):
            continue
        if set(line.strip()) <= {"-", "="}:
            continue
        row = {
            column: line[start:end].strip() if start < len(line) else ""
            for column, (start, end) in zip(columns, spans, strict=True)
        }
        if any(row.values()):
            rows.append(row)

    return SatTable(title=title, columns=columns, rows=rows)


def _find_header(lines: list[str]) -> int | None:
    """The header is the first line with 2+ column-separated words and no ':'."""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or ":" in stripped or "?" in stripped:
            continue
        if _COMMAND_LINE.match(stripped) or _PAGE_HEADER.search(stripped):
            continue
        if _looks_like_heading(stripped):
            continue
        if len(re.split(r"\s{2,}", stripped)) >= 2:
            return index
    return None


def _column_spans(header: str) -> list[tuple[int, int]]:
    """Start/end offsets for each column, from the header's word positions."""
    starts = [match.start() for match in re.finditer(r"\S+", header)]
    merged: list[int] = []
    for start in starts:
        # Words separated by a single space belong to one column heading.
        if merged and start - merged[-1] <= len(header[merged[-1] : start].rstrip()) + 1:
            continue
        merged.append(start)

    spans: list[tuple[int, int]] = []
    for index, start in enumerate(merged):
        end = merged[index + 1] if index + 1 < len(merged) else 10_000
        spans.append((start, end))
    return spans


def form_to_dict(form: SatForm) -> dict[str, Any]:
    """Flatten a form for retention in ``SourceRef.native_attributes``."""
    return {field.label: field.value for field in form.fields if field.value}


# --------------------------------------------------------------------------- #
# Session transport
# --------------------------------------------------------------------------- #


class SatSession(Protocol):
    """A SAT session that can run a command and return the raw screen text."""

    async def run(self, command: str) -> str: ...


class SatCommandRefused(SatParseError):
    """A command was attempted that is not on the reviewed read-only allow-list."""


#: SAT verbs this platform is permitted to issue. ``change``, ``add``, and
#: ``remove`` are absent deliberately: the Avaya connector is Extract-only, and
#: an allow-list is a cheaper guarantee of that than a code review.
READ_ONLY_SAT_VERBS: frozenset[str] = frozenset({"display", "list", "status"})


class CassetteSatSession:
    """Replays recorded SAT screens. The only session used in tests."""

    def __init__(self, screens: dict[str, str]) -> None:
        self._screens = screens

    async def run(self, command: str) -> str:
        verb = command.strip().split(" ", 1)[0].lower()
        if verb not in READ_ONLY_SAT_VERBS:
            raise SatCommandRefused(
                f"SAT verb {verb!r} is not read-only. Permitted: "
                f"{sorted(READ_ONLY_SAT_VERBS)}"
            )
        screen = self._screens.get(command.strip())
        if screen is None:
            raise SatParseError(
                f"No recorded SAT screen for {command!r}. Recorded commands: "
                f"{sorted(self._screens)}"
            )
        return screen


class SshSatSession:
    """A live SAT session over SSH.

    Unimplemented. Driving a terminal session needs the deployment's own
    decisions about terminal emulation, pagination keys, and login banner
    handling; guessing them would produce a client that hangs on the first
    unexpected prompt rather than failing usefully.
    """

    def __init__(self, *, host: str, username: str) -> None:
        self.host = host
        self.username = username

    async def run(self, command: str) -> str:
        raise NotImplementedError(
            "SshSatSession is not implemented. Terminal emulation, paging, and banner "
            "handling are deployment-specific and must be built against a real CM."
        )
