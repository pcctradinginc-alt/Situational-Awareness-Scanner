"""
vorfeld.py
==========
Non-filing **early-signal** sources, unified as change-detection adapters. Each
fetches a current state, diffs it against a persistent snapshot, and emits typed
:class:`VorfeldSignal` deltas — so the radar reacts to a *change*, not a level.

Three sources (all free, public, legally fetchable; injectable offline/live):

  * **ADV / IAPD** (`AdvIapdAdapter`) — relevant advisers/funds by CRD: AUM moves,
    new control persons, new private funds, office moves. (The "ADV/IAPD-Änderungen"
    requirement — NOT an EDGAR filing, a separate adapter, context-grade.)
  * **Job postings** (`JobPostingsAdapter`) — Greenhouse/Lever public boards of
    tracked firms/portfolio companies: NEW roles classified into thesis clusters,
    a hiring proxy for a theme shift.
  * **Domain / website watch** (`DomainWatchAdapter`) — content-hash change of
    official fund pages: a new LP/fund name or product page is an early footprint.

Every signal is advisory (`needs_human_review=True`) and CONTEXT-grade: it never
asserts a confirmed capital move. Media is never a source here. The first time a
target is seen it only establishes a BASELINE (no alert); deltas fire thereafter.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional, Protocol

from .proxy_map import classify_clusters

logger = logging.getLogger("coi.person.vorfeld")


# ── injectable HTTP layer ───────────────────────────────────────────────────
class Fetcher(Protocol):
    def get(self, url: str) -> Optional[str]: ...


@dataclass
class UrllibFetcher:                                    # pragma: no cover - network
    user_agent: str = "SA-Scanner research (info@pcctradinginc.com)"
    timeout_s: int = 15

    def get(self, url: str) -> Optional[str]:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception as exc:
            logger.warning("vorfeld fetch failed (%s): %s", url, exc)
            return None


@dataclass
class FixtureFetcher:
    """Offline fetch — maps a target key to ``<fixtures>/vorfeld/<sub>/<key>``."""
    fixtures_dir: Path

    def read(self, sub: str, name: str) -> Optional[str]:
        path = Path(self.fixtures_dir) / "vorfeld" / sub / name
        if not path.exists():
            logger.info("no vorfeld fixture %s/%s", sub, name)
            return None
        return path.read_text(encoding="utf-8")

    def get(self, url: str) -> Optional[str]:           # not URL-keyed offline
        return None


# ── persistent snapshot store (previous observed state) ─────────────────────
@dataclass
class SnapshotStore:
    path: Optional[Path]
    data: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None) -> "SnapshotStore":
        p = Path(path) if path else None
        data: dict = {}
        if p and p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        return cls(path=p, data=data)

    def get(self, key: str) -> Optional[dict]:
        return self.data.get(key)

    def set(self, key: str, value: dict) -> None:
        self.data[key] = value

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True),
                             encoding="utf-8")


# ── the signal ──────────────────────────────────────────────────────────────
@dataclass
class VorfeldSignal:
    source: str                 # adv_iapd | job_postings | domain_watch
    principal: str              # thiel | aschenbrenner | ""
    entity: str
    kind: str                   # aum_change | new_control_person | new_role | ...
    headline: str
    detail: str = ""
    cluster: str = ""           # thesis cluster, if classifiable
    url: str = ""
    change_date: str = ""
    age_days: Optional[int] = None
    content_hash: str = ""      # namespaced dedup key
    needs_human_review: bool = True
    triple: dict = field(default_factory=dict)
    brief: dict = field(default_factory=dict)    # 5-section action brief
    ev: dict = field(default_factory=dict)       # forward-EV hard-gate verdict

    def to_dict(self) -> dict:
        return asdict(self)


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


# ── ADV / IAPD ──────────────────────────────────────────────────────────────
class AdvIapdAdapter:
    """Diff an adviser's ADV/IAPD summary vs the prior snapshot.

    Live endpoint (best-effort): ``api.adviserinfo.sec.gov/search/firm/<CRD>``.
    The fixture/contract shape is a simplified summary dict:
        {firm_name, aum, employees, control_persons:[...], private_funds:[...],
         main_office}
    """
    LIVE = "https://api.adviserinfo.sec.gov/search/firm/"
    AUM_PCT = 0.10              # report AUM moves >= 10%

    def __init__(self, fetcher: Fetcher, fixture: Optional[FixtureFetcher] = None):
        self.fetcher = fetcher
        self.fixture = fixture

    def _fetch(self, crd: str) -> Optional[dict]:
        raw = (self.fixture.read("adv", f"{crd}.json") if self.fixture
               else self.fetcher.get(f"{self.LIVE}{crd}"))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def changes(self, advisers: list[dict], snapshot: SnapshotStore,
                as_of: date) -> list[VorfeldSignal]:
        out: list[VorfeldSignal] = []
        for a in advisers or []:
            crd = str(a.get("crd", "")).strip()
            if not crd:
                continue
            cur = self._fetch(crd)
            if not cur:
                continue
            key = f"adv:{crd}"
            name = cur.get("firm_name") or a.get("name", crd)
            principal = a.get("principal", "")
            prev = snapshot.get(key)
            snapshot.set(key, cur)
            if prev is None:
                continue                          # baseline only, no alert
            out.extend(self._diff(crd, name, principal, prev, cur, as_of))
        return out

    def _diff(self, crd, name, principal, prev, cur, as_of) -> list[VorfeldSignal]:
        sigs: list[VorfeldSignal] = []

        def sig(kind, headline, detail):
            return VorfeldSignal(
                source="adv_iapd", principal=principal, entity=name, kind=kind,
                headline=headline, detail=detail,
                url=f"https://adviserinfo.sec.gov/firm/summary/{crd}",
                change_date=as_of.isoformat(), age_days=0,
                content_hash=f"adv:{crd}:" + _hash(kind, detail))

        p_aum, c_aum = prev.get("aum"), cur.get("aum")
        if p_aum and c_aum and p_aum > 0:
            d = (c_aum - p_aum) / p_aum
            if abs(d) >= self.AUM_PCT:
                sigs.append(sig("aum_change",
                                f"{name}: ADV AUM {d:+.0%}",
                                f"AUM {p_aum:,.0f} → {c_aum:,.0f}"))
        for who in set(cur.get("control_persons", [])) - set(prev.get("control_persons", [])):
            sigs.append(sig("new_control_person",
                            f"{name}: new control person — {who}", who))
        for f in set(cur.get("private_funds", [])) - set(prev.get("private_funds", [])):
            sigs.append(sig("new_private_fund",
                            f"{name}: new private fund — {f}", f))
        if prev.get("main_office") and cur.get("main_office") \
                and prev["main_office"] != cur["main_office"]:
            sigs.append(sig("office_move",
                            f"{name}: office moved",
                            f"{prev['main_office']} → {cur['main_office']}"))
        return sigs


# ── job postings (Greenhouse / Lever) ───────────────────────────────────────
class JobPostingsAdapter:
    """Detect NEW roles on public job boards and classify them into thesis
    clusters — a hiring proxy for a theme shift."""
    GH = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    LEVER = "https://api.lever.co/v0/postings/{token}?mode=json"

    def __init__(self, fetcher: Fetcher, fixture: Optional[FixtureFetcher] = None):
        self.fetcher = fetcher
        self.fixture = fixture

    def _fetch(self, company: dict) -> list[dict]:
        token = company.get("token", "")
        provider = company.get("provider", "greenhouse")
        raw = (self.fixture.read("jobs", f"{token}.json") if self.fixture
               else self.fetcher.get(
                   (self.GH if provider == "greenhouse" else self.LEVER)
                   .format(token=token)))
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        rows = data.get("jobs", data) if isinstance(data, dict) else data
        out = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            out.append({
                "id": str(r.get("id") or r.get("absolute_url") or r.get("title", "")),
                "title": str(r.get("title") or r.get("text", "")).strip(),
                "url": str(r.get("absolute_url") or r.get("hostedUrl") or ""),
            })
        return out

    def changes(self, companies: list[dict], snapshot: SnapshotStore,
                as_of: date) -> list[VorfeldSignal]:
        out: list[VorfeldSignal] = []
        for c in companies or []:
            token = c.get("token", "")
            if not token:
                continue
            jobs = self._fetch(c)
            key = f"jobs:{token}"
            prev_ids = set((snapshot.get(key) or {}).get("ids", []))
            cur_ids = [j["id"] for j in jobs]
            snapshot.set(key, {"ids": cur_ids})
            if not prev_ids:
                continue                          # baseline only
            principal = c.get("principal", "")
            name = c.get("name", token)
            for j in jobs:
                if j["id"] in prev_ids:
                    continue
                clusters = classify_clusters(j["title"])
                dom = max(clusters, key=clusters.get) if clusters else ""
                out.append(VorfeldSignal(
                    source="job_postings", principal=principal, entity=name,
                    kind="new_role", headline=f"{name}: new role — {j['title']}",
                    detail=j["title"], cluster=dom, url=j["url"],
                    change_date=as_of.isoformat(), age_days=0,
                    content_hash=f"job:{token}:" + _hash(j["id"])))
        return out


# ── domain / website watch ──────────────────────────────────────────────────
# A site_change should fire on a NEW *footprint* (a fund/LP/SPV/product page
# appearing), NOT on cosmetic churn — a retitled essay, a rotated date, an
# analytics nonce. Two guards make that real:
#   1. SIGNIFICANCE GATE — extract footprint terms (fund vehicles, LP/SPV,
#      raise/close language) and alert only when a term appears that was not in
#      the prior snapshot. Permanent page furniture ("capital", a standing fund
#      name) is in the baseline, so it never re-fires; only NOVELTY does.
#   2. TITLE IGNORE-LIST — a per-site `ignore_titles` whitelist suppresses a
#      change whose new <title> matches a known, expected heading (e.g. an essay
#      that simply got a new section title). Auditable, explicit override.
# A volatile-text normalization (digits/nonces stripped) makes the change-hash
# represent structural content, so trivial churn no longer flips it. Sites that
# genuinely want the old "any content change" behaviour set alert_on_any_change.
_FOOTPRINT_TERMS = (
    "new fund", "limited partner", "limited partnership", "general partner",
    "spv", "special purpose vehicle", "feeder fund", "now raising",
    "first close", "final close", "fund close", "anchor lp", "anchor investor",
    "now investing", "introducing", "announcing", "launching", "new vehicle",
)
_FUND_NUM_RE = re.compile(r"\bfund\s+(?:[ivxlcdm]{1,7}|\d{1,3})\b", re.IGNORECASE)
_VOLATILE_RE = re.compile(r"\b[0-9a-f]{16,}\b|\d+")   # hex nonces & digit runs


class DomainWatchAdapter:
    """Change-detect an official page and alert only on a NEW fund/LP/product
    footprint — not on cosmetic churn.

    Stores ONLY a structural hash, a short title snippet, and the set of
    footprint terms seen, never the page body."""
    _TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
    _TAG_RE = re.compile(r"<[^>]+>")

    def __init__(self, fetcher: Fetcher, fixture: Optional[FixtureFetcher] = None):
        self.fetcher = fetcher
        self.fixture = fixture

    @staticmethod
    def _slug(url: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", url.lower()).strip("_")[:60]

    def _fetch(self, url: str) -> Optional[str]:
        if self.fixture:
            return self.fixture.read("domain", f"{self._slug(url)}.html")
        return self.fetcher.get(url)

    @staticmethod
    def _footprint_terms(text_lower: str, extra: list[str]) -> set[str]:
        vocab = list(_FOOTPRINT_TERMS) + [str(t).lower() for t in (extra or [])]
        terms = {t for t in vocab if t and t in text_lower}
        terms.update(m.strip() for m in _FUND_NUM_RE.findall(text_lower))
        return terms

    @staticmethod
    def _title_ignored(title: str, ignore: list[str]) -> bool:
        t = (title or "").lower()
        return any(str(x).lower() in t for x in (ignore or []) if str(x).strip())

    def changes(self, sites: list[dict], snapshot: SnapshotStore,
                as_of: date) -> list[VorfeldSignal]:
        out: list[VorfeldSignal] = []
        for s in sites or []:
            url = s.get("url", "")
            if not url:
                continue
            body = self._fetch(url)
            if body is None:
                continue
            text = " ".join(self._TAG_RE.sub(" ", body).split())
            text_lower = text.lower()
            terms = self._footprint_terms(text_lower, s.get("footprint_terms", []))
            # hash on volatile-stripped text → trivial churn no longer flips it
            h = _hash(_VOLATILE_RE.sub("", text_lower))
            m = self._TITLE_RE.search(body)
            title = (m.group(1).strip() if m else "")[:120]
            key = f"dom:{self._slug(url)}"
            prev = snapshot.get(key)
            snapshot.set(key, {"hash": h, "title": title, "terms": sorted(terms)})
            if prev is None:
                continue                          # baseline only, no alert
            if self._title_ignored(title, s.get("ignore_titles", [])):
                continue                          # known/expected heading
            changed = prev.get("hash") != h
            new_terms = sorted(terms - set(prev.get("terms", [])))
            fire = changed and (s.get("alert_on_any_change") or bool(new_terms))
            if not fire:
                continue
            detail = (f"new footprint term(s): {', '.join(new_terms)} · "
                      if new_terms else "content changed · ")
            detail += f"title now: {title}" if title else "no title"
            out.append(VorfeldSignal(
                source="domain_watch", principal=s.get("principal", ""),
                entity=s.get("entity", url), kind="site_change",
                headline=f"{s.get('entity', url)}: official page changed",
                detail=detail,
                url=url, change_date=as_of.isoformat(), age_days=0,
                content_hash=f"dom:{self._slug(url)}:" + h))
        return out


# ── certificate transparency (brand-new fund domains) ───────────────────────
def _age_days(iso: str, as_of: date) -> Optional[int]:
    try:
        return (as_of - date.fromisoformat(iso[:10])).days
    except (ValueError, TypeError):
        return None


class CertTransparencyAdapter:
    """Discover BRAND-NEW domains/subdomains via public Certificate Transparency
    logs (crt.sh) — often the earliest footprint of a forming fund/LP/project,
    weeks before any SEC filing or press.

    For each configured name pattern we read every logged certificate, collect
    the (sub)domains, and emit a ``new_domain`` signal for any domain not in the
    prior snapshot whose certificate is RECENT. A new cert is weak evidence (could
    be a vendor/parking/subdomain) → always ``needs_human_review``.
    """
    LIVE = "https://crt.sh/?q=%25{q}%25&output=json"

    def __init__(self, fetcher: Fetcher, fixture: Optional[FixtureFetcher] = None,
                 recent_days: int = 45):
        self.fetcher = fetcher
        self.fixture = fixture
        self.recent_days = recent_days

    @staticmethod
    def _slug(q: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", q.lower()).strip("_")[:60]

    def _fetch(self, q: str) -> list:
        from urllib.parse import quote
        raw = (self.fixture.read("cert", f"{self._slug(q)}.json") if self.fixture
               else self.fetcher.get(self.LIVE.format(q=quote(q))))
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    @staticmethod
    def _domains(certs: list) -> dict:
        """domain → earliest not_before date seen (ISO yyyy-mm-dd)."""
        out: dict[str, str] = {}
        for c in certs or []:
            if not isinstance(c, dict):
                continue
            names = set()
            for line in str(c.get("name_value", "")).splitlines():
                names.add(line)
            if c.get("common_name"):
                names.add(str(c["common_name"]))
            nb = str(c.get("not_before", ""))[:10]
            for n in names:
                d = n.strip().lower().lstrip("*.").strip()
                if not d or " " in d or "." not in d:
                    continue
                if d not in out or (nb and nb < out[d]):
                    out[d] = nb
        return out

    def changes(self, patterns: list[dict], snapshot: SnapshotStore,
                as_of: date) -> list[VorfeldSignal]:
        out: list[VorfeldSignal] = []
        for p in patterns or []:
            q = (p.get("q") if isinstance(p, dict) else str(p)) or ""
            if not q:
                continue
            principal = p.get("principal", "") if isinstance(p, dict) else ""
            excludes = [str(x).lower() for x in (p.get("exclude", [])
                        if isinstance(p, dict) else [])]
            cur = {d: nb for d, nb in self._domains(self._fetch(q)).items()
                   if not any(x in d for x in excludes)}
            key = f"cert:{self._slug(q)}"
            prev = snapshot.get(key)
            snapshot.set(key, {"domains": sorted(cur)})
            if prev is None:
                continue                              # baseline only, no alert
            prev_domains = set(prev.get("domains", []))
            for d in sorted(set(cur) - prev_domains):
                age = _age_days(cur[d], as_of)
                if age is not None and age > self.recent_days:
                    continue                          # old domain newly indexed
                clusters = classify_clusters(d.replace("-", " ").replace(".", " "))
                dom_cluster = max(clusters, key=clusters.get) if clusters else ""
                out.append(VorfeldSignal(
                    source="cert_transparency", principal=principal, entity=d,
                    kind="new_domain",
                    headline=f"NEW domain seen in CT logs — {d}",
                    detail=f"matches “{q}”, first certificate {cur[d] or 'n/a'}",
                    cluster=dom_cluster, url=f"https://crt.sh/?q={d}",
                    change_date=as_of.isoformat(), age_days=age,
                    content_hash=f"cert:{self._slug(q)}:" + _hash(d)))
        return out


# ── orchestration ───────────────────────────────────────────────────────────
def collect_vorfeld(cfg, mode: str, fixtures_dir, snapshot: SnapshotStore,
                    as_of: date) -> list[VorfeldSignal]:
    """Run every enabled non-filing vorfeld source against its snapshot."""
    from ..config_loader import load_yaml
    es = load_yaml("early_sources", getattr(cfg, "config_dir", None)) or {}
    live = (mode == "live")
    fx = None if live else FixtureFetcher(Path(fixtures_dir))
    net = UrllibFetcher() if live else FixtureFetcher(Path(fixtures_dir))

    out: list[VorfeldSignal] = []
    adv = es.get("adv_iapd", {})
    if adv.get("enabled"):
        out += AdvIapdAdapter(net, fx).changes(
            adv.get("advisers", []), snapshot, as_of)
    jobs = es.get("job_postings", {})
    if jobs.get("enabled"):
        out += JobPostingsAdapter(net, fx).changes(
            jobs.get("companies", []), snapshot, as_of)
    dom = es.get("domain_watch", {})
    if dom.get("enabled"):
        out += DomainWatchAdapter(net, fx).changes(
            dom.get("sites", []), snapshot, as_of)
    cert = es.get("cert_transparency", {})
    if cert.get("enabled"):
        out += CertTransparencyAdapter(
            net, fx, recent_days=int(cert.get("recent_days", 45))).changes(
            cert.get("patterns", []), snapshot, as_of)
    return out
