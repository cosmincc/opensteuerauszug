"""Look up ISIN / valor for ticker symbols via OpenFIGI names and the Kursliste.

Some brokers (Revolut, Fidelity, Schwab) export ticker symbols only. The
Kursliste, which is the authority for Swiss tax values, lists securities by
ISIN, valor and name but has no tickers. This module bridges the two:

1. :class:`OpenFigiClient` asks the free OpenFIGI mapping API for the
   company name behind a ticker (OpenFIGI does not return ISINs). Answers are
   cached on disk so repeated runs make no network calls.
2. :class:`KurslisteNameIndex` reads the names of shares, funds and
   derivatives from a downloaded Kursliste (SQLite or XML).
3. :func:`match_ticker` scores the Kursliste entries against the OpenFIGI
   name and reports how confident the best match is.

Name matching is heuristic: share classes, ADRs and abbreviated names can
mislead it. Callers must let the user confirm a match before using it; the
``identifiers lookup`` command does that and writes accepted matches to the
security identifiers CSV.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import lxml.etree as ET
import requests

from opensteuerauszug.kursliste.converter import read_metadata_value

logger = logging.getLogger(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

# Words that carry no identity in security names (legal forms, share wording).
_STOPWORDS = {
    "THE",
    "INC",
    "INCORPORATED",
    "CORP",
    "CORPORATION",
    "CO",
    "COMPANY",
    "LTD",
    "LIMITED",
    "PLC",
    "SA",
    "AG",
    "NV",
    "SE",
    "AB",
    "ASA",
    "SPA",
    "LLC",
    "LP",
    "HOLDING",
    "HOLDINGS",
    "GROUP",
    "SP",
    "SPON",
    "SPONS",
    "SPONSORED",
    "ADR",
    "ADS",
    "DR",
    "REG",
    "REGISTERED",
    "SHS",
    "SHARES",
    "SHARE",
    "ORD",
    "ORDINARY",
    "COMMON",
    "STOCK",
    "STK",
    "NPV",
    "CL",
    "CLASS",
    "NEW",
    "AND",
}

# Kursliste security groups worth matching against tickers.
_MATCH_GROUPS = {"SHARE", "FUND", "DEVT"}

_CLASS_IN_FIGI_RE = re.compile(r"(?:-|\b)(?:CL|CLASS)\s*([A-Z])\b")
_CLASS_IN_KL_RE = re.compile(
    r"(?:-([A-Z])-|\bCLASS\s+([A-Z])\b|\bCL\.?\s*([A-Z])\b|\bSERIES\s+([A-Z])\b)"
)
_DEPOSITARY_RE = re.compile(r"\b(ADR|ADS|DEPOSITARY|DEPOSITORY)\b")


# ---------------------------------------------------------------------------
# Name handling
# ---------------------------------------------------------------------------


def name_tokens(name: str) -> list[str]:
    """Identity-bearing tokens of a security name, upper case."""
    text = name.upper().replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return [t for t in text.split() if t not in _STOPWORDS and len(t) > 1 or t.isdigit()]


def share_class(name: str, figi: bool) -> str | None:
    """Share class letter mentioned in a name (e.g. ``ALPHABET INC-CL A``)."""
    text = name.upper()
    m = (_CLASS_IN_FIGI_RE if figi else _CLASS_IN_KL_RE).search(text)
    if not m:
        return None
    return next(g for g in m.groups() if g)


def _tokens_match(a: str, b: str) -> bool:
    if a == b:
        return True
    # Abbreviations: "MANUF" / "TOT" vs "MANUFACTURING" / "TOTAL".
    short, long_ = sorted((a, b), key=len)
    if len(short) < 3 or short[0] != long_[0] or short.isdigit() or long_.isdigit():
        return False
    if long_.startswith(short):
        return True
    # Consonant-style abbreviations: "STK" / "STOCK", "MGMT" / "MANAGEMENT".
    rest = iter(long_)
    return all(ch in rest for ch in short)


def name_similarity(figi_name: str, kursliste_name: str) -> float:
    """Similarity in [0, 1] between an OpenFIGI name and a Kursliste name.

    OpenFIGI names are short and abbreviated, so the score rewards a
    Kursliste name that contains every OpenFIGI token (allowing prefixes),
    and falls back to a character-level ratio otherwise.
    """
    a = name_tokens(figi_name)
    b = name_tokens(kursliste_name)
    if not a or not b:
        return 0.0
    covered = sum(1 for t in a if any(_tokens_match(t, u) for u in b))
    coverage = covered / len(a)
    # Penalise Kursliste names with many extra words (e.g. a different company
    # whose long name merely contains the short one).
    extra = max(0, len(b) - covered)
    ratio = SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
    if coverage == 1.0:
        return max(ratio, 0.9 - 0.05 * min(extra, 4))
    return min(0.85, 0.6 * coverage + 0.4 * ratio)


# ---------------------------------------------------------------------------
# Kursliste index
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KurslisteEntry:
    name: str
    isin: str | None
    valor: int | None
    currency: str | None
    country: str | None
    group: str | None
    appendix: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.name} {self.appendix}" if self.appendix else self.name


def _entry_from_attrib(attrib: dict[str, str]) -> KurslisteEntry | None:
    name = attrib.get("securityName") or attrib.get("institutionName")
    if not name:
        return None
    group = attrib.get("securityGroup")
    if group and group not in _MATCH_GROUPS:
        return None
    valor_text = attrib.get("valorNumber")
    return KurslisteEntry(
        name=name,
        isin=attrib.get("isin") or None,
        valor=int(valor_text) if valor_text and valor_text.isdigit() else None,
        currency=attrib.get("currency"),
        country=attrib.get("country"),
        group=group,
        appendix=attrib.get("securityAppendix"),
    )


_ATTR_RE = re.compile(rb'([A-Za-z]+)="([^"]*)"')
_START_TAG_RE = re.compile(rb"<[^>]*>", re.DOTALL)


def _attrib_from_blob(blob: bytes) -> dict[str, str]:
    """Attributes of the root start tag of an XML blob, without a full parse."""
    m = _START_TAG_RE.search(blob)
    if not m:
        return {}
    return {
        k.decode(): html.unescape(v.decode("utf-8", errors="replace"))
        for k, v in _ATTR_RE.findall(m.group(0))
    }


class KurslisteNameIndex:
    """Names of Kursliste securities, searchable by token."""

    def __init__(self, entries: Iterable[KurslisteEntry]):
        self.entries: list[KurslisteEntry] = list(entries)
        self._by_token: dict[str, list[int]] = defaultdict(list)
        for i, e in enumerate(self.entries):
            for t in set(name_tokens(e.name)):
                self._by_token[t[:4]].append(i)

    def __len__(self) -> int:
        return len(self.entries)

    def candidates(self, name: str) -> list[KurslisteEntry]:
        """Entries sharing at least one token prefix with *name*."""
        idx: set[int] = set()
        for t in set(name_tokens(name)):
            idx.update(self._by_token.get(t[:4], ()))
        return [self.entries[i] for i in sorted(idx)]

    # -- loading -----------------------------------------------------------

    @classmethod
    def from_directory(cls, directory: str | Path, tax_year: int) -> KurslisteNameIndex:
        """Load the Kursliste for *tax_year* from a directory (SQLite preferred)."""
        directory = Path(directory)
        if not directory.is_dir():
            raise FileNotFoundError(f"Kursliste directory not found: {directory}")
        for db in sorted(directory.glob("*.sqlite")):
            try:
                year = read_metadata_value(db, "tax_year")
            except Exception:  # noqa: BLE001 - unreadable DBs are just skipped
                year = None
            if year is not None and int(year) == tax_year:
                return cls.from_sqlite(db, tax_year)
        for xml in sorted(directory.glob("*.xml")):
            if str(tax_year) in xml.name:
                return cls.from_xml(xml)
        raise FileNotFoundError(
            f"No Kursliste for {tax_year} in {directory}. Download it with "
            f"'opensteuerauszug kursliste download --year {tax_year}'."
        )

    @classmethod
    def from_sqlite(cls, path: str | Path, tax_year: int) -> KurslisteNameIndex:
        conn = sqlite3.connect(str(path))
        try:
            rows = conn.execute(
                "SELECT security_object_blob FROM securities WHERE tax_year = ?", (tax_year,)
            )
            entries = []
            for (blob,) in rows:
                if isinstance(blob, str):
                    blob = blob.encode()
                if not blob or not blob.lstrip().startswith(b"<"):
                    continue  # legacy JSON blobs are not supported here
                entry = _entry_from_attrib(_attrib_from_blob(blob))
                if entry:
                    entries.append(entry)
        finally:
            conn.close()
        return cls(entries)

    @classmethod
    def from_xml(cls, path: str | Path) -> KurslisteNameIndex:
        entries = []
        for _, elem in ET.iterparse(str(path), events=("end",)):
            tag = ET.QName(elem).localname
            if tag in ("share", "fund", "derivative", "bond"):
                entry = _entry_from_attrib(dict(elem.attrib))
                if entry:
                    entries.append(entry)
                elem.clear()
        return cls(entries)


# ---------------------------------------------------------------------------
# OpenFIGI
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FigiRecord:
    name: str
    security_type: str | None = None
    exch_code: str | None = None
    market_sector: str | None = None


EXCHANGE_FOR_CURRENCY = {"USD": "US", "GBP": "LN", "CHF": "SW"}


class OpenFigiClient:
    """Minimal OpenFIGI v3 mapping client with an on-disk cache.

    Without an API key OpenFIGI allows 25 requests per minute with 10 jobs
    each; with a key (free registration) 25 requests per 6 seconds with 100
    jobs each.
    """

    CACHE_TTL = timedelta(days=180)

    def __init__(
        self,
        cache_path: Path | None,
        api_key: str | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cache_path = cache_path
        self.api_key = api_key
        self.session = session or requests.Session()
        self.sleep = sleep
        self._cache: dict[str, Any] = self._load_cache()

    @property
    def _batch_size(self) -> int:
        return 100 if self.api_key else 10

    @property
    def _pause(self) -> float:
        return 0.25 if self.api_key else 2.5

    def _load_cache(self) -> dict[str, Any]:
        if not self.cache_path or not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Ignoring unreadable OpenFIGI cache %s", self.cache_path)
            return {}

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache, indent=1, sort_keys=True), "utf-8")

    @staticmethod
    def _key(ticker: str, exch_code: str | None) -> str:
        return f"{exch_code or '*'}:{ticker.upper()}"

    def _cached(self, key: str) -> list[FigiRecord] | None:
        item = self._cache.get(key)
        if not item:
            return None
        fetched = datetime.fromisoformat(item["fetched"])
        if datetime.now(UTC) - fetched > self.CACHE_TTL:
            return None
        return [FigiRecord(**r) for r in item["records"]]

    def lookup(
        self, tickers: Sequence[tuple[str, str | None]]
    ) -> dict[tuple[str, str | None], list[FigiRecord]]:
        """Return OpenFIGI records for each ``(ticker, exchange code)``."""
        result: dict[tuple[str, str | None], list[FigiRecord]] = {}
        todo = []
        for ticker, exch in dict.fromkeys(tickers):
            cached = self._cached(self._key(ticker, exch))
            if cached is not None:
                result[(ticker, exch)] = cached
            else:
                todo.append((ticker, exch))

        for start in range(0, len(todo), self._batch_size):
            batch = todo[start : start + self._batch_size]
            jobs = [
                {"idType": "TICKER", "idValue": t, **({"exchCode": e} if e else {})}
                for t, e in batch
            ]
            answers = self._post(jobs)
            now = datetime.now(UTC).isoformat()
            for (ticker, exch), answer in zip(batch, answers):
                records = [
                    FigiRecord(
                        name=d.get("name") or "",
                        security_type=d.get("securityType"),
                        exch_code=d.get("exchCode"),
                        market_sector=d.get("marketSector"),
                    )
                    for d in (answer.get("data") or [])
                    if d.get("name")
                ]
                if "error" in answer:
                    logger.warning("OpenFIGI error for %s: %s", ticker, answer["error"])
                    continue  # do not cache errors
                result[(ticker, exch)] = records
                self._cache[self._key(ticker, exch)] = {
                    "fetched": now,
                    "records": [asdict(r) for r in records],
                }
            self._save_cache()
            if start + self._batch_size < len(todo):
                self.sleep(self._pause)
        return result

    def _post(self, jobs: list[dict[str, str]]) -> list[dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-OPENFIGI-APIKEY"] = self.api_key
        for attempt in range(5):
            response = self.session.post(OPENFIGI_URL, json=jobs, headers=headers, timeout=30)
            if response.status_code == 429:
                wait = float(response.headers.get("Retry-After", 0) or 0) or 10.0 * (attempt + 1)
                logger.info("OpenFIGI rate limit reached; waiting %.0fs", wait)
                self.sleep(wait)
                continue
            response.raise_for_status()
            answers = response.json()
            if not isinstance(answers, list) or len(answers) != len(jobs):
                raise ValueError("Unexpected OpenFIGI response")
            return answers
        raise RuntimeError("OpenFIGI rate limit persisted after 5 attempts")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    entry: KurslisteEntry
    score: float


@dataclass
class MatchResult:
    ticker: str
    figi_name: str | None
    candidates: list[Candidate] = field(default_factory=list)
    confident: bool = False
    reason: str = ""

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


HIGH_SCORE = 0.85
MIN_SCORE = 0.6
MIN_MARGIN = 0.08


def match_ticker(
    ticker: str,
    records: Sequence[FigiRecord],
    index: KurslisteNameIndex,
    currency: str | None = None,
    limit: int = 3,
) -> MatchResult:
    """Rank Kursliste entries for *ticker* given its OpenFIGI records."""
    names = list(dict.fromkeys(r.name for r in records if r.name))
    if not names:
        return MatchResult(ticker, None, reason="OpenFIGI does not know this ticker")
    figi_name = names[0]
    is_depositary = any(
        (r.security_type or "").upper() in ("ADR", "GDR") or _DEPOSITARY_RE.search(r.name.upper())
        for r in records
    )
    wanted_class = share_class(figi_name, figi=True)

    scored: dict[KurslisteEntry, float] = {}
    for name in names:
        for entry in index.candidates(name):
            if currency and entry.currency and entry.currency != currency:
                continue
            score = name_similarity(name, entry.name)
            if score < MIN_SCORE:
                continue
            full = entry.full_name.upper()
            kl_class = share_class(full, figi=False)
            if wanted_class and kl_class:
                score += 0.05 if kl_class == wanted_class else -0.3
            kl_depositary = bool(_DEPOSITARY_RE.search(full))
            if is_depositary != kl_depositary:
                score -= 0.1
            if currency == "USD" and entry.isin and entry.isin.startswith("US"):
                score += 0.02
            scored[entry] = max(scored.get(entry, 0.0), min(score, 1.0))

    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0].name))
    result = MatchResult(ticker, figi_name, [Candidate(e, round(s, 3)) for e, s in ranked[:limit]])
    if not ranked:
        result.reason = "no similar name in the Kursliste"
        return result
    best = ranked[0][1]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if best < HIGH_SCORE:
        result.reason = "best name match is weak"
    elif best - runner_up < MIN_MARGIN:
        result.reason = "several Kursliste entries match about equally well"
    elif not (ranked[0][0].isin or ranked[0][0].valor):
        result.reason = "best match has neither ISIN nor valor"
    else:
        result.confident = True
    return result
