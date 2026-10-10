"""Ledger of positions that have already been reported by alert email.

The alert step used to dedupe by ``event_id`` only. Every syndicated copy of
the same article (MSN, Globe and Mail, Yahoo, ...) has its own id, so one
position produced dozens of emails. This module resolves each event to the
*position* it is about (issuer level) and remembers which positions were
already reported, so an email goes out exactly once per newly opened position.

The ledger lives in ``alert_state.json`` under ``reported_positions``::

    {"name": "Nebius Group N.V.", "names": ["nebius"], "tickers": ["NBIS"],
     "cusips": [], "headlines": ["..."], "first_reported_at": "...",
     "via": "13f", "event_id": "..."}

Identity is fuzzy on purpose: an event matches an entry when a ticker or CUSIP
overlaps, when the entry's issuer name occurs in the event text, or when the
headline is a near-copy of one already reported. LLM ticker guesses for the
same company vary (NBIS / NEBU / NEBIUS), so the name match is what carries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Corporate-form tokens that carry no identity ("Core Scientific Inc New").
_SUFFIX_TOKENS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "lt",
    "limited", "plc", "nv", "n", "v", "sa", "ag", "se", "lp", "llc", "group",
    "holdings", "holding", "hldg", "hldgs", "new", "com", "adr", "ads", "class",
    "ordinary", "shares", "the",
}
# Single-word names that are too generic to match on their own.
_GENERIC_TOKENS = {
    "energy", "digital", "applied", "core", "group", "technology", "technologies",
    "systems", "infrastructure", "capital", "global", "american", "first", "stock",
    "taiwan", "united", "general", "national", "advanced", "pacific", "genius",
    "bloom", "riot", "hive", "keel", "vista", "summit", "pioneer", "frontier",
}
# Words that end a company name guessed from a headline ("Bloom Energy Stock Before").
_COMPANY_STOP = {
    "this", "that", "the", "a", "an", "my", "his", "its", "stock", "stocks", "shares",
    "before", "after", "why", "here", "and", "as", "just", "in", "of", "for", "with",
    "ahead", "amid", "while", "what", "how", "is", "to", "on", "at", "ai", "new",
    "significant", "stake", "position", "bold", "major", "large", "big", "huge",
    "massive", "more", "another", "these", "two", "three",
}
_PUBLISHER_RE = re.compile(r"\s+[-–—|]\s+[^-–—|]{2,40}$")
_TITLE_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "and", "to", "for", "is", "its", "just",
    "s", "why", "here", "heres", "this", "that", "by", "with", "as", "at", "up",
    "leopold", "aschenbrenner", "aschenbrenners", "situational", "awareness",
    "lp", "fund", "stock", "stake",
}
# "... stake in <Company>", "... bought 5.6% of <Company>" → company name guess.
# The prepositional forms are tried first: after a bare verb the next words are
# often not the company ("Acquires Significant Stake in ...").
_COMPANY_NAME = r"\s+((?:[A-Z][\w&.'’-]*)(?:\s+[A-Z0-9][\w&.'’-]*){0,3})"
_COMPANY_RES = [
    re.compile(r"(?i:stake in|position in|investment in|invests in|invested in|bet on|"
               r"bets on|shares of|%\s+of)" + _COMPANY_NAME),
    re.compile(r"(?i:buys|bought|acquires|acquired)" + _COMPANY_NAME),
]
_HEADLINE_DUP_THRESHOLD = 0.6
_MAX_HEADLINES = 5


def normalize_name(name: str | None) -> str:
    """Lower-case issuer name without punctuation and corporate-form tokens."""
    tokens = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
    while tokens and tokens[-1] in _SUFFIX_TOKENS:
        tokens.pop()
    return " ".join(tokens)


def _name_phrases(norm: str) -> set[str]:
    """Phrases that identify an issuer in running text.

    13F issuer names are truncated ("TAIWAN SEMICONDUCTOR MANUFAC"), so the
    first two words are matched as well as the full name. The first word alone
    counts only when it is long enough and not generic.
    """
    tokens = norm.split()
    if not tokens:
        return set()
    first = tokens[0]
    if len(tokens) == 1:
        return {first} if len(first) >= 4 and first not in _GENERIC_TOKENS else set()
    phrases = {norm, " ".join(tokens[:2])}
    if len(first) >= 6 and first not in _GENERIC_TOKENS:
        phrases.add(first)  # "Micron" for MICRON TECHNOLOGY INC
    return phrases


def _headline_tokens(title: str | None) -> frozenset[str]:
    text = _PUBLISHER_RE.sub("", title or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    return frozenset(
        t for t in re.sub(r"[^a-z0-9% ]+", " ", text).split() if t not in _TITLE_STOPWORDS
    )


def _similar(a: frozenset[str], b: frozenset[str]) -> bool:
    if len(a) < 3 or len(b) < 3:
        return False
    return len(a & b) / len(a | b) >= _HEADLINE_DUP_THRESHOLD


def _guess_company(title: str | None) -> str:
    title = _PUBLISHER_RE.sub("", title or "")
    for pattern in _COMPANY_RES:
        for m in pattern.finditer(title):
            words: list[str] = []
            for word in m.group(1).split():
                if word.lower().strip(".,'’") in _COMPANY_STOP:
                    break
                words.append(word)
            if words:
                return " ".join(words).strip(" .,'’")
    return ""


@dataclass
class PositionRef:
    """What an event (or a 13F row) says about which position it concerns."""

    name: str = ""
    tickers: set[str] = field(default_factory=set)
    cusips: set[str] = field(default_factory=set)
    text: str = ""       # free text searched for known issuer names
    headline: str = ""   # compared against already-reported headlines

    @property
    def identified(self) -> bool:
        return bool(normalize_name(self.name) or self.tickers or self.cusips)

    @property
    def label(self) -> str:
        ticker = sorted(self.tickers)[0] if self.tickers else ""
        if self.name and ticker:
            return f"{self.name} ({ticker})"
        return self.name or ticker or (sorted(self.cusips)[0] if self.cusips else "?")


def ref_from_row(row: dict) -> PositionRef:
    """PositionRef for a position-model row (common stock or option)."""
    return PositionRef(
        name=row.get("issuer") or row.get("underlying") or "",
        tickers={row["ticker"]} if row.get("ticker") else set(),
        cusips={row["cusip"]} if row.get("cusip") else set(),
    )


def ref_from_event(evt: dict) -> PositionRef:
    """PositionRef for an event from events.jsonl."""
    src = (evt.get("sources") or [{}])[0]
    sig = evt.get("signal_type")
    if sig == "public_statement":
        title = evt.get("summary", "")
        return PositionRef(
            name=src.get("llm_company") or _guess_company(title),
            tickers={t for t in (evt.get("ticker_guess") or []) if t},
            text=f"{title} {src.get('llm_quote', '')}",
            headline=title,
        )
    if sig == "sec_fts_mention":
        return PositionRef(
            name=re.sub(r"\s*\([^)]*\)\s*$", "", src.get("entity", "")).strip(),
            tickers=set(evt.get("ticker_guess") or []),
        )
    cusip = src.get("issuer_cusip") or ""
    ticker = src.get("issuer_ticker") or ""
    name = src.get("issuer_name") or ""
    return PositionRef(
        name="" if name == "unknown issuer" else name,
        tickers={ticker} if ticker else set(),
        cusips={cusip} if cusip else set(),
    )


def active_refs(model: dict) -> list[PositionRef]:
    """Positions held in the latest 13F quarter (stock and live options)."""
    rows = [r for r in model.get("common_stock", []) if r.get("shares_latest", 1)]
    rows += [r for r in model.get("options", []) if r.get("notional_latest_usd", 0) > 0]
    return [ref_from_row(r) for r in rows]


class Ledger:
    """Positions already reported. Wraps the list stored in the alert state."""

    def __init__(self, entries: list[dict] | None = None) -> None:
        self.entries: list[dict] = entries if entries is not None else []

    def find(self, ref: PositionRef) -> dict | None:
        ref_norm = normalize_name(ref.name)
        ref_phrases = _name_phrases(ref_norm)
        text = f" {normalize_name(ref.text)} " if ref.text else ""
        head = _headline_tokens(ref.headline) if ref.headline else frozenset()
        for entry in self.entries:
            if ref.tickers & set(entry.get("tickers", [])):
                return entry
            if ref.cusips & set(entry.get("cusips", [])):
                return entry
            for norm in entry.get("names", []):
                phrases = _name_phrases(norm)
                if ref_norm and (ref_norm == norm or ref_phrases & phrases):
                    return entry
                if text and any(f" {p} " in text for p in phrases):
                    return entry
            if head and any(_similar(head, _headline_tokens(h)) for h in entry.get("headlines", [])):
                return entry
        return None

    def add(self, ref: PositionRef, *, via: str, now: str, event_id: str = "") -> dict:
        """Record ``ref`` as reported; merges into an existing entry if one matches.

        A headline-derived ref (news) only contributes its headline to an
        existing entry: its name and ticker are guesses and would widen the
        entry to unrelated companies.
        """
        entry = self.find(ref)
        is_new = entry is None
        if entry is None:
            entry = {
                "name": ref.name or ref.label, "names": [], "tickers": [], "cusips": [],
                "headlines": [], "first_reported_at": now, "via": via, "event_id": event_id,
            }
            self.entries.append(entry)
        if is_new or not ref.headline:
            norm = normalize_name(ref.name)
            if norm and norm not in entry["names"]:
                entry["names"].append(norm)
            entry["tickers"] = sorted(set(entry["tickers"]) | ref.tickers)
            entry["cusips"] = sorted(set(entry["cusips"]) | ref.cusips)
        if ref.headline and ref.headline not in entry["headlines"]:
            entry["headlines"] = (entry["headlines"] + [ref.headline])[-_MAX_HEADLINES:]
        return entry

    def release_exited(self, model: dict) -> list[str]:
        """Drop entries the latest 13F shows as fully exited.

        Only entries reported *before* that quarter's report date are released,
        so a later re-entry counts as a newly opened position again while a
        purchase reported after quarter end stays on the ledger.
        """
        report_date = (model.get("summary") or {}).get("report_date") or ""
        if not report_date:
            return []
        active = Ledger()
        for ref in active_refs(model):
            active.add(ref, via="13f", now="")
        released: list[str] = []
        for row in model.get("exits", []):
            ref = ref_from_row(row)
            if active.find(ref):
                continue  # CUSIP changed across quarters — still held
            entry = self.find(ref)
            if entry and (entry.get("first_reported_at") or "9999")[:10] < report_date:
                self.entries.remove(entry)
                released.append(entry["name"])
        return released
