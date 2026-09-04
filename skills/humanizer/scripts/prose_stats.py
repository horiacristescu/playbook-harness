"""Measure the machine tells in Markdown prose.

Reports banned expressions first (they are the first thing a reader notices),
then sentence, paragraph, enumeration, contrast, and punctuation statistics
against a baseline taken from well-regarded project READMEs.

Standard library only; no shebang on purpose, because the installed copy
carries a Playbook ownership marker on line 1. Usage:

    python3 prose_stats.py FILE [FILE ...] [--json] [--banned PATH] [--no-banned]

Exit status is 0; the numbers are for revision, not for gating a build.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Baseline. Frozen 2026-08-29 from the README.md of ripgrep, jj, litestream,
# tailscale, fish-shell, and just (pinned copies live in tests/fixtures/prose
# in the Playbook repository). A value outside a range is a prompt to look,
# not a verdict. Known outliers in the corpus itself: litestream and
# tailscale are under 25 sentences, so their spread is noisy; ripgrep and
# just use one- and two-sentence paragraphs throughout (paragraph sd 0.6-0.7).
#
# Two rows are rules from the humanizer skill rather than corpus ranges:
# em dashes and fancy characters. Human READMEs do use them (jj has 2.6 em
# dashes per 1000 words), but the skill asks for zero because readers now
# take them as a machine tell.
#
# Triad share is reported but never flagged: in the corpus 67-100 percent of
# human series are exactly three items, so the rule of three is not a tell
# on its own; the rate of series is.
# ---------------------------------------------------------------------------
BASELINE = {
    # metric: (low, high, "direction that reads as machine", "ref"|"rule")
    "sentence_words_mean": (11.0, 24.0, "low", "ref"),
    "sentence_words_sd": (6.0, 15.0, "low", "ref"),
    "sentence_words_max": (28.0, 90.0, "low", "ref"),
    "paragraph_sentences_sd": (0.4, 3.0, "low", "ref"),
    "series_rate": (0.0, 0.16, "high", "ref"),
    "contrast_rate": (0.0, 0.05, "high", "ref"),
    "participle_ending_rate": (0.0, 0.04, "high", "ref"),
    "coda_rate": (0.0, 0.25, "high", "ref"),
    "msttr_100": (0.60, 0.85, "low", "ref"),
    "em_dashes_per_1k_words": (0.0, 0.0, "high", "rule"),
    "fancy_chars": (0.0, 0.0, "high", "rule"),
}

# Below this many sentences the spread statistics are noise; flags are
# suppressed and the report says so.
MIN_SENTENCES = 20

FANCY = {
    "—": "em dash",
    "–": "en dash",
    "“": "curly quote",
    "”": "curly quote",
    "‘": "curly apostrophe",
    "’": "curly apostrophe",
    "…": "ellipsis",
    "→": "arrow",
    "•": "bullet",
    " ": "non-breaking space",
}

CONTRAST_PATTERNS = [
    re.compile(r"\bis\s?n[o']t\s+(?:just\s+|only\s+|merely\s+)?[^.;:]{1,60}?,\s*(?:it\s?'?s|it is|but)\b", re.I),
    re.compile(r"\bnot\s+(?:just\s+|only\s+|merely\s+)?[^.;:]{1,60}?,\s*but\b", re.I),
    re.compile(r"\bnot\s+(?:just\s+|only\s+|merely\s+)?[^.;:]{1,60}?\s+but\s+(?:also\s+)?", re.I),
    re.compile(r",\s*not\s+(?:a|an|the|just|only|merely)?\s*[\w-]+[.;]", re.I),
    re.compile(r"\brather than\b", re.I),
    re.compile(r"\binstead of\b", re.I),
]

CODA_OPENERS = re.compile(
    r"^(?:This|That|These|Those|It)\s+(?:is|are|was|were|makes|means|matters|keeps|gives|lets|shows)\b",
)

PARTICIPLE_ENDING = re.compile(
    r",\s*(?:\w+ly\s+)?(?:ensuring|highlighting|emphasizing|reflecting|showcasing|"
    r"underscoring|demonstrating|allowing|enabling|making|creating|providing|"
    r"offering|helping|leading|resulting|contributing|fostering|supporting|"
    r"signaling|marking|leaving|giving|setting)\b[^.]*[.!?]$",
    re.I,
)


@dataclass
class Doc:
    path: str
    text: str
    lines: list[str]
    paragraphs: list[list[str]]  # paragraphs as lists of sentences
    sentences: list[str]
    words: list[str]
    bullets: int = 0
    header_then_bullets: int = 0
    banned: list[tuple[int, str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Markdown to prose
# ---------------------------------------------------------------------------

def strip_markdown(text: str) -> tuple[str, int, int]:
    """Return (prose, bullet_count, header_then_bullets)."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"```.*?```", "\n", text, flags=re.S)
    text = re.sub(r"~~~.*?~~~", "\n", text, flags=re.S)
    text = re.sub(r"<[^>\n]+>", "", text)
    lines = text.split("\n")
    out: list[str] = []
    bullets = 0
    header_then_bullets = 0
    prev_was_header = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^#{1,6}\s", stripped):
            prev_was_header = True
            out.append("")
            continue
        if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", line):
            bullets += 1
            if prev_was_header:
                header_then_bullets += 1
            prev_was_header = False
            out.append("")
            continue
        if stripped.startswith("|") or re.match(r"^[-=~*_]{3,}$", stripped):
            out.append("")
            prev_was_header = stripped == "" and prev_was_header
            continue
        if stripped.startswith(">"):
            stripped = stripped.lstrip("> ").strip()
        if re.match(r"^\[[^\]]+\]:\s*\S+", stripped):
            out.append("")
            continue
        if stripped == "":
            out.append("")
            continue
        prev_was_header = False
        out.append(stripped)
    prose = "\n".join(out)
    prose = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", prose)
    prose = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", prose)
    prose = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", prose)
    prose = re.sub(r"`([^`\n]*)`", r"\1", prose)
    prose = re.sub(r"(\*\*|__)(.*?)\1", r"\2", prose)
    prose = re.sub(r"(?<!\w)[*_]([^*_\n]+)[*_](?!\w)", r"\1", prose)
    return prose, bullets, header_then_bullets


# A period that does not end a sentence: common abbreviations, initialisms
# such as U.S. or e.g., and a single capital letter (an initial).
ABBREV = re.compile(
    r"(?:\b(?:e\.g|i\.e|vs|etc|Mr|Mrs|Ms|Dr|St|No|cf|approx|ca|fig|al)\.|"
    r"(?:\b[A-Za-z]\.){2,}|\b[A-Z]\.)$",
    re.I,
)


def split_sentences(paragraph: str) -> list[str]:
    """Split on ., !, ? followed by whitespace. The next sentence may start
    with a lowercase word (a command name, a code identifier), so no case
    check is applied; instead, splits after abbreviations and initialisms
    are merged back."""
    flat = " ".join(paragraph.split())
    parts = re.split(r"(?<=[.!?])[\"')\]]?\s+(?=\S)", flat)
    merged: list[str] = []
    for part in parts:
        if merged and ABBREV.search(merged[-1]):
            merged[-1] = merged[-1] + " " + part
        else:
            merged.append(part)
    return [p.strip() for p in merged if len(p.split()) >= 2]


FENCE = re.compile(r"^\s*(```|~~~)")


def blank_fences(raw: str) -> str:
    """Replace the content of fenced code blocks with empty lines so line
    numbers survive while fenced text is excluded from character and phrase
    scans."""
    out = []
    open_fence: str | None = None
    for line in raw.split("\n"):
        match = FENCE.match(line)
        if match and (open_fence is None or match.group(1) == open_fence):
            open_fence = None if open_fence else match.group(1)
            out.append("")
            continue
        out.append("" if open_fence else line)
    return "\n".join(out)


def load(path: Path, banned: list[str]) -> Doc:
    raw = path.read_text(encoding="utf-8")
    prose, bullets, htb = strip_markdown(raw)
    paragraphs: list[list[str]] = []
    for block in re.split(r"\n\s*\n", prose):
        if not block.strip():
            continue
        sents = split_sentences(block)
        if sents:
            paragraphs.append(sents)
    sentences = [s for p in paragraphs for s in p]
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", " ".join(sentences).lower())
    scanned = blank_fences(raw)
    doc = Doc(path=str(path), text=scanned, lines=scanned.split("\n"),
              paragraphs=paragraphs, sentences=sentences, words=words,
              bullets=bullets, header_then_bullets=htb)
    if banned:
        doc.banned = find_banned(scanned, banned)
    return doc


# ---------------------------------------------------------------------------
# Banned expressions
# ---------------------------------------------------------------------------

def load_banned(path: Path | None) -> list[str]:
    if path is None:
        path = Path(__file__).with_name("banned.txt")
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def find_banned(raw: str, banned: list[str]) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    patterns = [
        (expr, re.compile(r"(?<![\w-])" + re.escape(expr) + r"(?![\w-])", re.I))
        for expr in banned
    ]
    for number, line in enumerate(raw.split("\n"), start=1):
        for expr, pattern in patterns:
            if pattern.search(line):
                hits.append((number, expr, line.strip()[:100]))
    return hits


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

# A series is comma-separated items closed by and/or/nor. With an Oxford
# comma the conjunction follows the last comma directly ("a, b, and c");
# without one it sits one to five words after the last comma ("a, b and
# c"). A sentence that merely has commas and an "and" somewhere later is
# not counted.
SERIES_OXFORD = re.compile(
    r"((?:[^,;:.()]+,\s+){2,})(?:and|or|nor)\b"
)
SERIES_PLAIN = re.compile(
    r"((?:[^,;:.()]+,\s+){1,})(?:(?!and\b|or\b|nor\b)\S+\s+){1,5}?(?:and|or|nor)\b"
)
# A short opening segment that starts with a preposition, subordinator, or
# adverb ("In practice,", "After setup,", "If the build fails,", "Usually,")
# is an introductory adjunct, not the first item of a list.
ADJUNCT = re.compile(
    r"^\s*(?:in|on|at|for|after|before|with|without|by|over|under|during|"
    r"since|as|if|when|whenever|because|although|though|while|unless|until|"
    r"once|where|whereas|from|to|of|like|unlike|despite|beyond|through|"
    r"\w+ly|however|still|then|so|also|first|second|third|finally|today|now)"
    r"\b",
    re.I,
)


def _strip_adjunct(items: list[str]) -> list[str]:
    if items and len(items[0].split()) <= 6 and ADJUNCT.match(items[0]):
        return items[1:]
    return items


def series_lengths(sentence: str) -> list[int]:
    """Item counts of each comma series with three or more items.

    An introductory adjunct before the list ("In practice, a, b, and c") is
    dropped before counting; a compound sentence whose first clause is
    subordinate ("If the build fails, the job stops, and the log is kept")
    then has only two items and is not counted.
    """
    lengths = []
    spans: list[tuple[int, int]] = []
    for pattern, extra in ((SERIES_OXFORD, 1), (SERIES_PLAIN, 2)):
        for match in pattern.finditer(sentence):
            if any(a <= match.start() < b for a, b in spans):
                continue
            items = _strip_adjunct(
                [x for x in match.group(1).split(",") if x.strip()]
            )
            count = len(items) + extra
            if count < 3:
                continue
            spans.append(match.span())
            lengths.append(count)
    return lengths


def word_count(sentence: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", sentence))


def msttr(words: list[str], window: int = 100) -> float | None:
    """Mean segmental type-token ratio over fixed windows."""
    if len(words) < window:
        return None
    ratios = []
    for start in range(0, len(words) - window + 1, window):
        segment = words[start:start + window]
        ratios.append(len(set(segment)) / window)
    return sum(ratios) / len(ratios)


def sd(values: list[float]) -> float:
    return statistics.pstdev(values) if len(values) > 1 else 0.0


def measure(doc: Doc) -> dict:
    sent_words = [word_count(s) for s in doc.sentences]
    para_sents = [len(p) for p in doc.paragraphs]
    para_words = [sum(word_count(s) for s in p) for p in doc.paragraphs]
    series = [n for s in doc.sentences for n in series_lengths(s)]
    hist = {"3": 0, "4": 0, "5+": 0}
    for n in series:
        hist["3" if n == 3 else "4" if n == 4 else "5+"] += 1
    sentences_with_series = sum(1 for s in doc.sentences if series_lengths(s))
    contrasts = sum(
        1 for s in doc.sentences if any(p.search(s) for p in CONTRAST_PATTERNS)
    )
    participle = sum(1 for s in doc.sentences if PARTICIPLE_ENDING.search(s))
    codas = sum(
        1 for p in doc.paragraphs if len(p) >= 2 and CODA_OPENERS.match(p[-1])
    )
    fancy: dict[str, int] = {}
    for char, name in FANCY.items():
        n = doc.text.count(char)
        if n:
            fancy[name] = fancy.get(name, 0) + n
    total_words = sum(sent_words) or 1
    n_sent = len(doc.sentences) or 1
    n_para = len(doc.paragraphs) or 1
    return {
        "path": doc.path,
        "words": sum(sent_words),
        "sentences": len(doc.sentences),
        "paragraphs": len(doc.paragraphs),
        "sentence_words_mean": statistics.mean(sent_words) if sent_words else 0.0,
        "sentence_words_sd": sd(sent_words),
        "sentence_words_min": min(sent_words) if sent_words else 0,
        "sentence_words_max": max(sent_words) if sent_words else 0,
        "paragraph_sentences_mean": statistics.mean(para_sents) if para_sents else 0.0,
        "paragraph_sentences_sd": sd(para_sents),
        "paragraph_words_mean": statistics.mean(para_words) if para_words else 0.0,
        "paragraph_words_sd": sd(para_words),
        "series_rate": sentences_with_series / n_sent,
        "series_count": len(series),
        "series_lengths": hist,
        "triad_share": (hist["3"] / len(series)) if series else 0.0,
        "contrast_rate": contrasts / n_sent,
        "contrast_count": contrasts,
        "participle_ending_rate": participle / n_sent,
        "coda_rate": codas / n_para,
        "em_dashes_per_1k_words": 1000 * doc.text.count("—") / total_words,
        "fancy_chars": sum(fancy.values()),
        "fancy_detail": fancy,
        "bullets": doc.bullets,
        "header_then_bullets": doc.header_then_bullets,
        "msttr_100": msttr(doc.words),
        "banned": [
            {"line": n, "expression": e, "text": t} for n, e, t in doc.banned
        ],
    }


def with_flags(result: dict) -> dict:
    result["flags"] = {
        key: flag(key, result[key], result["sentences"]) for key in BASELINE
    }
    result["small_sample"] = result["sentences"] < MIN_SENTENCES
    return result


def flag(metric: str, value: float | None, sentences: int) -> str:
    """LOW/HIGH when the value points in the machine direction, low/high
    when outside the range the other way, ok inside, empty when there is no
    baseline or too little text for a reference range."""
    if value is None or metric not in BASELINE:
        return ""
    low, high, bad, kind = BASELINE[metric]
    if sentences < MIN_SENTENCES and kind == "ref":
        return ""
    if value < low:
        return "LOW" if bad == "low" else "low"
    if value > high:
        return "HIGH" if bad == "high" else "high"
    return "ok"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

ROWS = [
    ("sentence_words_mean", "sentence length, mean words", "{:.1f}"),
    ("sentence_words_sd", "sentence length, sd", "{:.1f}"),
    ("sentence_words_max", "sentence length, max", "{:.0f}"),
    ("paragraph_sentences_mean", "paragraph length, mean sentences", "{:.1f}"),
    ("paragraph_sentences_sd", "paragraph length, sd", "{:.1f}"),
    ("series_rate", "sentences with a 3+ item series", "{:.0%}"),
    ("triad_share", "share of those series that are triads (info)", "{:.0%}"),
    ("contrast_rate", "contrast constructions (not X but Y, rather than)", "{:.0%}"),
    ("participle_ending_rate", "sentences ending in a participle chain", "{:.0%}"),
    ("coda_rate", "paragraphs ending on This/That/It is...", "{:.0%}"),
    ("em_dashes_per_1k_words", "em dashes per 1000 words", "{:.1f}"),
    ("fancy_chars", "fancy characters (curly quotes, ellipsis, arrows)", "{:.0f}"),
    ("msttr_100", "lexical diversity (MSTTR-100)", "{:.2f}"),
]


def render(result: dict) -> str:
    out = [f"== {result['path']}"]
    if result["banned"]:
        out.append(f"  banned expressions: {len(result['banned'])} (the first tell; fix before the numbers)")
        for hit in result["banned"]:
            out.append(f"    {hit['line']:>4}: [{hit['expression']}] {hit['text']}")
    else:
        out.append("  banned expressions: none")
    out.append(
        f"  {result['words']} words, {result['sentences']} sentences, "
        f"{result['paragraphs']} paragraphs, {result['bullets']} bullet lines"
        + (f", {result['header_then_bullets']} headers followed straight by bullets"
           if result["header_then_bullets"] else "")
    )
    if result["sentences"] < MIN_SENTENCES:
        out.append(
            f"  small sample: under {MIN_SENTENCES} sentences, reference flags suppressed"
        )
    for key, label, fmt in ROWS:
        value = result[key]
        shown = "n/a" if value is None else fmt.format(value)
        mark = flag(key, value, result["sentences"])
        ref = ""
        if key in BASELINE:
            low, high, _bad, kind = BASELINE[key]
            ref = f"  {kind} {fmt.format(low)}..{fmt.format(high)}"
        out.append(f"  {label:<52} {shown:>8}  {mark:<4}{ref}")
    if result["series_count"]:
        h = result["series_lengths"]
        out.append(
            f"  series lengths: 3 items x{h['3']}, 4 items x{h['4']}, 5+ items x{h['5+']}"
        )
    if result["fancy_detail"]:
        out.append("  fancy: " + ", ".join(f"{k} x{v}" for k, v in result["fancy_detail"].items()))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--banned", type=Path, help="banned expressions file (default: banned.txt beside this script)")
    parser.add_argument("--no-banned", action="store_true", help="skip the banned expression scan")
    args = parser.parse_args(argv)
    banned = [] if args.no_banned else load_banned(args.banned)
    results = []
    for path in args.files:
        if not path.is_file():
            print(f"skip (not a file): {path}", file=sys.stderr)
            continue
        results.append(with_flags(measure(load(path, banned))))
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print("\n\n".join(render(r) for r in results))
        print(
            "\nUPPER-CASE flags point in the machine direction; lower-case ones are"
            " outside the range the other way. 'ref' ranges come from six"
            " well-regarded project READMEs; 'rule' rows are the skill's own rule."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
