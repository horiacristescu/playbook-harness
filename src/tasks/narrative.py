"""Narrative: read `.agent/chat_log.md` as arcs over spans over comments.

The audience is the user, at a glance, over hours — "what happened while I was
away" — not an agent reading context. That drives every choice here: the
collapsed view is the product, time is legible, and extension is incremental.

Division of labour, deliberately strict:

    this module   parse the log, validate annotations, render HTML
    the agent     author the annotations (see `skills/narrative`)

Segmentation is judgment, so the CLI never guesses a boundary. It parses,
checks what the agent authored, and reports what does not add up.

Two properties of `chat_log.md` that this module exists to survive
(MIND_MAP [11]):

* it is live and append-only, so a line-count tail is a moving window —
  annotations pin their boundaries instead;
* entry IDs are neither unique nor stable — lifecycle rows reuse ids like
  ``S:discover``, and message counters reset across the log's history, so a
  boundary is an ``(id, ts)`` pair resolved sequentially.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA = 1

ANNOTATIONS_NAME = "annotations.json"
ENTRIES_NAME = "entries.json"
PAGE_NAME = "narrative.html"

_HEADER = re.compile(
    r"^\*\*\[(?P<id>[^\]]+)\]\*\*\s+"
    r"\[(?P<ts>[^\]]+)\]\s+"
    r"`(?P<role>[^`]+)`"
    r"(?:\s*\((?P<meta>[^)]*)\))?\s*$"
)

_TS_FORMATS = ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S")


class NarrativeError(Exception):
    """Something the user must fix — bad annotations, missing log, empty window."""


@dataclass(frozen=True)
class Entry:
    """One chat_log row: a human message, a gate closure, or a lifecycle event."""

    id: str
    ts: str
    role: str
    provider: str
    kind: str
    text: str

    @property
    def when(self) -> datetime | None:
        return parse_ts(self.ts)


@dataclass
class Span:
    """A continuous stretch of attention. Carries no outcome — an arc does."""

    object: str
    title: str
    description: str
    start: dict[str, str]
    end: dict[str, str]
    entries: list[Entry] = field(default_factory=list)


@dataclass
class Arc:
    """A thread of intent across spans. The outcome is what earns the layer."""

    title: str
    description: str
    outcome: str
    spans: list[Span] = field(default_factory=list)

    @property
    def entries(self) -> list[Entry]:
        return [e for s in self.spans for e in s.entries]


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def classify(entry_id: str) -> str:
    """`M####` human message, `G<task>:<n>` gate closure, `S:`/`T` lifecycle."""
    if entry_id.startswith("G"):
        return "gate"
    if entry_id.startswith(("S:", "T")):
        return "event"
    return "msg"


def parse_ts(value: str) -> datetime | None:
    text = value.strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # Trailing zone label we do not need to interpret precisely.
    parts = text.split()
    if len(parts) >= 2:
        try:
            return datetime.strptime(" ".join(parts[:2]), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return None


def parse(lines: Iterable[str]) -> list[Entry]:
    """Parse chat_log lines into entries, dropping bodiless rows.

    Bodiless rows are real: MIND_MAP [11] records that historical entries can
    have blank or duplicated bodies. They carry no narrative content, so they
    are dropped here rather than rendered as empty comments.
    """
    entries: list[Entry] = []
    current: dict[str, Any] | None = None
    body: list[str] = []

    def flush() -> None:
        if current is None:
            return
        text = "\n".join(body).strip()
        if text:
            entries.append(
                Entry(
                    id=current["id"],
                    ts=current["ts"],
                    role=current["role"],
                    provider=current["provider"],
                    kind=classify(current["id"]),
                    text=text,
                )
            )

    for line in lines:
        match = _HEADER.match(line)
        if match:
            flush()
            data = match.groupdict()
            meta = data.get("meta") or ""
            current = {
                "id": data["id"],
                "ts": data["ts"],
                "role": data["role"],
                "provider": meta.split("/")[0].strip() or "-",
            }
            body = []
        elif current is not None and line.strip() != "---":
            body.append(line)
    flush()
    return entries


def read_log(path: Path) -> list[str]:
    if not path.exists():
        raise NarrativeError(
            f"no chat log at {path}. Playbook writes it through the chat-log hook; "
            "a project that never enabled chat logging has no narrative to build."
        )
    return path.read_text(encoding="utf-8").splitlines()


def tail_window(lines: Sequence[str], count: int) -> list[Entry]:
    """Last `count` lines. A MOVING window — callers must say so."""
    return parse(lines[-count:])


def locate(entries: Sequence[Entry], anchor: dict[str, str], at_or_after: int) -> int | None:
    """First entry matching `anchor` at or after a position.

    Sequential resolution, not an id->index dict: ids repeat, so a dict silently
    keeps the last occurrence. The timestamp is a cross-check against counter
    resets, which make an id ambiguous across eras.
    """
    want_id = anchor["id"]
    want_ts = anchor.get("ts")
    for index in range(at_or_after, len(entries)):
        entry = entries[index]
        if entry.id != want_id:
            continue
        if want_ts and entry.ts.strip() != want_ts.strip():
            continue
        return index
    return None


# --------------------------------------------------------------------------
# annotations
# --------------------------------------------------------------------------


def _require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise NarrativeError(f"{where}: missing required field {key!r}")
    return mapping[key]


def _anchor(value: Any, where: str) -> dict[str, str]:
    if isinstance(value, str):
        # Tolerated shorthand: bare id, no counter-reset cross-check available.
        return {"id": value, "ts": ""}
    entry_id = _require(value, "id", where)
    return {"id": str(entry_id), "ts": str(value.get("ts", ""))}


def load_annotations(path: Path) -> list[Arc]:
    """Read and structurally validate annotations.json.

    Nesting arcs over spans makes 'span in no arc', 'span claimed twice' and
    'unknown span id' impossible by construction — the errors that a joined
    two-file layout had to detect at runtime.
    """
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise NarrativeError(f"{path} is not valid JSON: {exc}") from exc

    schema = raw.get("schema") if isinstance(raw, dict) else None
    if schema != SCHEMA:
        raise NarrativeError(
            f"{path}: schema {schema!r} is not supported (this runtime writes {SCHEMA})"
        )
    arcs_raw = _require(raw, "arcs", str(path))
    if not isinstance(arcs_raw, list):
        raise NarrativeError(f"{path}: arcs must be a list")

    arcs: list[Arc] = []
    for position, arc_raw in enumerate(arcs_raw):
        where = f"{path}: arc {position}"
        arc = Arc(
            title=str(_require(arc_raw, "title", where)),
            description=str(arc_raw.get("description", "")),
            outcome=str(arc_raw.get("outcome", "")),
        )
        spans_raw = _require(arc_raw, "spans", where)
        if not isinstance(spans_raw, list) or not spans_raw:
            raise NarrativeError(f"{where}: spans must be a non-empty list")
        for span_position, span_raw in enumerate(spans_raw):
            span_where = f"{where}, span {span_position}"
            arc.spans.append(
                Span(
                    object=str(span_raw.get("object", "")),
                    title=str(_require(span_raw, "title", span_where)),
                    description=str(span_raw.get("description", "")),
                    start=_anchor(_require(span_raw, "start", span_where), span_where),
                    end=_anchor(_require(span_raw, "end", span_where), span_where),
                )
            )
        arcs.append(arc)
    return arcs


def dump_annotations(arcs: Sequence[Arc], path: Path) -> None:
    payload = {
        "schema": SCHEMA,
        "arcs": [
            {
                "title": arc.title,
                "description": arc.description,
                "outcome": arc.outcome,
                "spans": [
                    {
                        "object": span.object,
                        "start": span.start,
                        "end": span.end,
                        "title": span.title,
                        "description": span.description,
                    }
                    for span in arc.spans
                ],
            }
            for arc in arcs
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def iter_spans(arcs: Sequence[Arc]) -> Iterator[Span]:
    for arc in arcs:
        yield from arc.spans


def bind(entries: Sequence[Entry], arcs: Sequence[Arc]) -> list[str]:
    """Attach entries to spans in log order. Returns non-fatal warnings.

    Resolution is forward-only, so spans stay contiguous and a repeated id binds
    to the occurrence the author meant. Anything that cannot be anchored is an
    error: a silently mis-anchored span would misattribute real history.
    """
    warnings: list[str] = []
    cursor = 0
    covered: set[int] = set()

    for arc in arcs:
        for span in arc.spans:
            start = locate(entries, span.start, cursor)
            if start is None:
                raise NarrativeError(
                    f"span {span.title!r}: start {span.start['id']} not found at or after "
                    f"position {cursor}. Either the window excludes it, or annotations are "
                    "out of log order, or a message-counter reset moved it."
                )
            end = locate(entries, span.end, start)
            if end is None:
                raise NarrativeError(
                    f"span {span.title!r}: end {span.end['id']} not found at or after its start"
                )
            span.entries = list(entries[start : end + 1])
            covered.update(range(start, end + 1))
            cursor = end + 1

    # Only *interior* gaps are a problem. History before the first annotation and
    # comments after the last one are simply not narrated yet — that is the normal
    # incremental state, not an authoring mistake, and warning about it would make
    # every run on a long log noisy enough to ignore.
    if covered:
        interior = range(min(covered), max(covered) + 1)
        gaps = [entries[i].id for i in interior if i not in covered]
        if gaps:
            shown = ", ".join(gaps[:8]) + (" …" if len(gaps) > 8 else "")
            warnings.append(
                f"{len(gaps)} comment(s) sit inside the narrated range but belong to "
                f"no span: {shown}. Extend a span to cover them."
            )
    return warnings


def covered_bounds(entries: Sequence[Entry], arcs: Sequence[Arc]) -> tuple[int, int] | None:
    """Positions of the first and last annotated comment, or None if unannotated."""
    first: int | None = None
    cursor = 0
    for span in iter_spans(arcs):
        start = locate(entries, span.start, cursor)
        if start is None:
            continue
        end = locate(entries, span.end, start)
        end = start if end is None else end
        first = start if first is None else first
        cursor = end + 1
    return None if first is None else (first, cursor - 1)


def covered_window(entries: Sequence[Entry], arcs: Sequence[Arc]) -> list[Entry]:
    """The stretch the narrative actually describes, first annotation to last."""
    bounds = covered_bounds(entries, arcs)
    return [] if bounds is None else list(entries[bounds[0] : bounds[1] + 1])


def annotated_end(entries: Sequence[Entry], arcs: Sequence[Arc]) -> int:
    """Index just past the last annotated entry — where incremental work starts."""
    cursor = 0
    for span in iter_spans(arcs):
        start = locate(entries, span.start, cursor)
        if start is None:
            continue
        end = locate(entries, span.end, start)
        cursor = (end if end is not None else start) + 1
    return cursor


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

_CSS = """
:root{--bg:#0f1115;--panel:#171a21;--deep:#13161c;--line:#262b36;--fg:#dfe3ea;
--dim:#8b93a3;--msg:#7fb3ff;--gate:#5fd6a4;--event:#c9a227;--accent:#ff9d5c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:28px 32px 20px;border-bottom:1px solid var(--line)}
h1{margin:0 0 6px;font-size:20px;letter-spacing:.2px}
.sub{color:var(--dim);font-size:13px}
.legend{display:flex;gap:16px;margin-top:12px;font-size:12px;color:var(--dim);flex-wrap:wrap}
.dot{display:inline-block;width:8px;height:8px;border-radius:99px;margin-right:5px}
.wrap{max-width:1020px;margin:0 auto;padding:0 24px 80px}
.warn{margin:14px 0 0;padding:10px 13px;border-left:2px solid var(--event);
background:rgba(201,162,39,.07);color:#c9a227;font-size:12.5px}
.day{display:flex;align-items:center;gap:10px;margin:22px 0 8px;
color:var(--dim);font-size:11px;letter-spacing:.14em;text-transform:uppercase}
.day::after{content:"";flex:1;height:1px;background:var(--line)}
.arc{margin:10px 0;border:1px solid var(--line);border-radius:10px;background:var(--deep)}
.arc>summary{cursor:pointer;list-style:none;padding:16px 18px;outline:none}
.arc>summary::-webkit-details-marker{display:none}
.ahead{display:flex;gap:12px;align-items:baseline}
.ahead::before{content:"\\25b8";color:var(--accent);font-size:12px}
.arc[open] .ahead::before{content:"\\25be"}
.arc[open]>summary{border-bottom:1px solid var(--line)}
.arc>summary:hover{background:#181c23;border-radius:10px}
.arc[open]>summary:hover{border-radius:10px 10px 0 0}
.atitle{font-weight:650;font-size:17px;letter-spacing:.1px}
.outcome{color:#7e8798;margin:8px 0 0 15px;max-width:78ch;font-size:12.5px;
line-height:1.5;border-left:2px solid var(--gate);padding-left:9px}
.abody{padding:10px 14px 14px}
.span{margin:4px 0;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
.span>summary{cursor:pointer;list-style:none;padding:12px 16px;outline:none}
.span>summary::-webkit-details-marker{display:none}
.shead{display:flex;gap:12px;align-items:baseline}
.shead::before{content:"\\25b8";color:var(--accent);font-size:11px}
.span[open] .shead::before{content:"\\25be"}
.span[open]>summary{border-bottom:1px solid var(--line)}
.span>summary:hover{background:#1c2028;border-radius:8px}
.span[open]>summary:hover{border-radius:8px 8px 0 0}
.stitle{font-weight:600;font-size:15px}
.range{color:var(--dim);font-size:11.5px;margin-left:auto;white-space:nowrap;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.desc{color:#a8b0be;margin:6px 0 0 15px;max-width:78ch;font-size:13px;line-height:1.5}
.body{padding:12px 16px 14px}
.entry{margin:0 0 7px;padding-left:11px;border-left:2px solid var(--line)}
.entry.msg{border-left-color:var(--msg)}
.entry.gate{border-left-color:var(--gate)}
.entry.event{border-left-color:var(--event)}
.text{white-space:pre-wrap;word-wrap:break-word;margin:0;line-height:1.5}
.entry.msg .text{color:#eef1f6}
.entry.gate .text,.entry.event .text{color:#939bab;font-size:13px}
.more{cursor:pointer;color:var(--dim);font-size:13px;line-height:1.5;outline:none;list-style:none}
.more::-webkit-details-marker{display:none}
.more:hover{color:var(--accent)}
@media(max-width:640px){.wrap{padding:0 14px 60px}.range{margin-left:0}
.ahead,.shead{flex-wrap:wrap;gap:6px}}
"""

_PREVIEW = 260


def _esc(value: str) -> str:
    return html.escape(value, quote=False)


def humanize(delta_seconds: float) -> str:
    """Compact duration: the point is 11h vs 12m at a glance."""
    minutes = int(delta_seconds // 60)
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if not rest else f"{hours}h{rest:02d}"
    days, rest_hours = divmod(hours, 24)
    return f"{days}d" if not rest_hours else f"{days}d{rest_hours}h"


def timespan(entries: Sequence[Entry]) -> str:
    """`08-12 20:14 → 08-13 07:14 · 11h`, degrading when timestamps are unparseable."""
    if not entries:
        return ""
    first, last = entries[0], entries[-1]
    # Derive from timestamps, not list position: a hand-edited annotations file
    # can put entries out of order, and a negative duration would render as a
    # plausible-looking "<1m" instead of an obvious mistake.
    stamps = [e.when for e in entries if e.when is not None]
    start = min(stamps) if stamps else None
    end = max(stamps) if stamps else None
    if start is None or end is None:
        return f"{first.ts} → {last.ts}"
    same_day = start.date() == end.date()
    left = start.strftime("%m-%d %H:%M")
    right = end.strftime("%H:%M" if same_day else "%m-%d %H:%M")
    return f"{left} → {right} · {humanize((end - start).total_seconds())}"


def _entry_html(entry: Entry) -> str:
    """One compact line of plain text; the coloured rule carries the distinction."""
    text = entry.text if entry.kind == "msg" else " ".join(entry.text.split())
    if entry.kind != "msg" and len(text) > _PREVIEW:
        head, rest = text[:_PREVIEW], text[_PREVIEW:]
        inner = (
            f'<details><summary class="more">{_esc(head)}… '
            f"<span>[+{len(rest)}]</span></summary>"
            f'<p class="text">{_esc(text)}</p></details>'
        )
    else:
        inner = f'<p class="text">{_esc(text)}</p>'
    return f'<div class="entry {entry.kind}">{inner}</div>'


def _span_html(span: Span) -> str:
    return (
        '<details class="span"><summary>'
        f'<div class="shead"><span class="stitle">{_esc(span.title)}</span>'
        f'<span class="range">{_esc(timespan(span.entries))} · {len(span.entries)}</span></div>'
        f'<p class="desc">{_esc(span.description)}</p>'
        '</summary><div class="body">'
        + "".join(_entry_html(e) for e in span.entries)
        + "</div></details>"
    )


def _day_of(entries: Sequence[Entry]) -> str | None:
    when = entries[0].when if entries else None
    return when.strftime("%A %d %B %Y") if when else None


def _with_day_rules(blocks: Iterable[tuple[str | None, str]], previous: str | None = None) -> str:
    """Insert a day divider wherever the date changes.

    Days are orthogonal to arcs: an arc can run across midnight, so this is a
    rule drawn through the timeline at both levels rather than a nesting layer.
    """
    out: list[str] = []
    for day, html_block in blocks:
        if day and day != previous:
            out.append(f'<div class="day">{_esc(day)}</div>')
            previous = day
        out.append(html_block)
    return "".join(out)


def _arc_html(arc: Arc) -> str:
    entries = arc.entries
    outcome = f'<p class="outcome">{_esc(arc.outcome)}</p>' if arc.outcome else ""
    # An arc's own day is already announced above it, so only mark changes
    # *within* the arc — the midnight crossings.
    spans = list(reversed(arc.spans))
    body = _with_day_rules(
        ((_day_of(s.entries), _span_html(s)) for s in spans),
        previous=_day_of(spans[0].entries) if spans else None,
    )
    return (
        '<details class="arc"><summary>'
        f'<div class="ahead"><span class="atitle">{_esc(arc.title)}</span>'
        f'<span class="range">{_esc(timespan(entries))} · '
        f"{len(arc.spans)} spans · {len(entries)} comments</span></div>"
        f'<p class="desc">{_esc(arc.description)}</p>{outcome}'
        f'</summary><div class="abody">{body}</div></details>'
    )


def render(entries: Sequence[Entry], arcs: Sequence[Arc], warnings: Sequence[str] = ()) -> str:
    """Newest arc first; comments inside a span stay chronological.

    Day dividers are drawn between arcs wherever the date changes. Days are
    orthogonal to arcs — an arc can run across midnight — so the divider marks
    the transition rather than containing anything.
    """
    body = _with_day_rules(
        (_day_of(arc.entries), _arc_html(arc)) for arc in reversed(list(arcs))
    )
    warned = "".join(f'<p class="warn">{_esc(w)}</p>' for w in warnings)
    span_count = sum(len(a.spans) for a in arcs)
    covered = sum(len(a.entries) for a in arcs)
    head = (
        "<header><h1>Narrative</h1>"
        f'<div class="sub">{covered} of {len(entries)} comments · '
        f"{span_count} spans · {len(arcs)} arcs · {_esc(timespan(entries))}</div>"
        '<div class="legend">'
        '<span><i class="dot" style="background:var(--msg)"></i>you</span>'
        '<span><i class="dot" style="background:var(--gate)"></i>gate closed</span>'
        '<span><i class="dot" style="background:var(--event)"></i>lifecycle</span>'
        "</div></header>"
    )
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>Narrative</title><style>{_CSS}</style></head><body>"
        f'{head}<div class="wrap">{warned}{body}</div></body></html>'
    )


# --------------------------------------------------------------------------
# operations (what the CLI calls)
# --------------------------------------------------------------------------


@dataclass
class Report:
    """What one narrative run did, in terms the user can act on."""

    total: int
    annotated: int
    pending: list[Entry]
    warnings: list[str]
    page: Path | None = None
    annotations: Path | None = None

    @property
    def up_to_date(self) -> bool:
        return not self.pending

    def lines(self) -> list[str]:
        out: list[str] = []
        noun = "comment" if self.total == 1 else "comments"
        if self.annotated:
            out.append(f"{self.annotated} of {self.total} {noun} narrated")
        else:
            out.append(f"{self.total} {noun}, none narrated yet")
        if self.pending:
            first, last = self.pending[0], self.pending[-1]
            out.append(
                f"{len(self.pending)} new since the last annotation: "
                f"{first.id} ({first.ts}) → {last.id} ({last.ts})"
            )
        else:
            out.append("nothing new to narrate")
        out.extend(f"warning: {w}" for w in self.warnings)
        if self.page:
            out.append(f"page: {self.page}")
        return out


def narrative_dir(project: Path) -> Path:
    return project / ".agent" / "narrative"


def status(project: Path, lines_back: int | None = None) -> Report:
    """What is narrated, what is not. The default entry point — read-only."""
    log_lines = read_log(project / ".agent" / "chat_log.md")
    entries = parse(log_lines if lines_back is None else log_lines[-lines_back:])
    if not entries:
        raise NarrativeError(
            "no parsable comments in the chat log — it exists but holds no entries "
            "in the requested window"
        )
    directory = narrative_dir(project)
    arcs = load_annotations(directory / ANNOTATIONS_NAME)
    warnings: list[str] = []
    if arcs:
        warnings = bind(entries, arcs)
    cursor = annotated_end(entries, arcs)
    return Report(
        total=len(entries),
        annotated=sum(len(a.entries) for a in arcs),
        pending=list(entries[cursor:]),
        warnings=warnings,
        annotations=directory / ANNOTATIONS_NAME,
    )


def build(project: Path, lines_back: int | None = None) -> Report:
    """Render the page from existing annotations. Never invents annotations."""
    report = status(project, lines_back)
    log_lines = read_log(project / ".agent" / "chat_log.md")
    entries = parse(log_lines if lines_back is None else log_lines[-lines_back:])
    directory = narrative_dir(project)
    arcs = load_annotations(directory / ANNOTATIONS_NAME)
    if not arcs:
        raise NarrativeError(
            f"no annotations yet at {directory / ANNOTATIONS_NAME}. "
            "Narrative is authored, not inferred — run the `narrative` skill so an "
            "agent reads the comments and writes arcs and spans, then render."
        )
    bind(entries, arcs)
    directory.mkdir(parents=True, exist_ok=True)
    page = directory / PAGE_NAME
    # Render the narrated stretch, not the whole log: the page is the narrative,
    # and counting thousands of comments nobody has narrated tells the reader
    # nothing about what happened.
    page.write_text(render(covered_window(entries, arcs), arcs, report.warnings), encoding="utf-8")
    report.page = page
    return report


def dump_pending(project: Path, limit: int | None = None, lines_back: int | None = None) -> str:
    """Compact listing of un-narrated comments, for the authoring agent to read.

    One line per comment at low resolution — the agent opens full text only
    where the narrative needs it.
    """
    report = status(project, lines_back)
    pending = report.pending if limit is None else report.pending[:limit]
    rows = [
        f"{e.id}\t{e.ts}\t{e.kind}\t{' '.join(e.text.split())[:160]}" for e in pending
    ]
    if not rows:
        return "nothing new to narrate"
    header = f"# {len(report.pending)} un-narrated comment(s)"
    if limit is not None and len(report.pending) > limit:
        header += f" (showing first {limit})"
    return "\n".join([header, *rows])
