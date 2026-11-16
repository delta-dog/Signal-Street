"""Clustering of duplicates and follow-ups within a rolling window.

Hard keys (exchange announcement id, canonical media URL) are handled by the store:
an item whose (source, id) we already have is never re-processed. This module decides,
for a genuinely new item, whether it starts a cluster or joins one:

  lead      first item of a story; the only role that can alert
  dupe      same story again (NSE and BSE carrying one filing; the same wire copy in
            two outlets)
  followup  a media story about something we already have from an exchange/regulator
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

STOPWORDS = set("""a an and are as at be by for from has have in into is it its of on or
that the this to was were will with after over says said amid ltd limited india indian
""".split())


def title_tokens(title: str, *, strip_publisher: bool = False) -> list[str]:
    t = title or ""
    if strip_publisher and " - " in t:
        head, tail = t.rsplit(" - ", 1)
        if len(tail.split()) <= 4:          # Google News: "Headline - Publisher"
            t = head
    words = re.sub(r"[^a-z0-9]+", " ", t.lower()).split()
    return [w for w in words if w not in STOPWORDS and len(w) > 1]


def simhash(tokens: list[str]) -> int:
    feats = tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
    v = [0] * 64
    for f in feats:
        h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
        for i in range(64):
            v[i] += 1 if h >> i & 1 else -1
    return sum(1 << i for i in range(64) if v[i] > 0)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass
class Entry:
    cluster_id: int
    at: datetime
    source: str
    rank: int          # 1 exchange, 2 regulator, 3 wire, 4 media
    symbol: str | None
    label: str | None
    sh: int
    tokens: frozenset


class Clusterer:
    def __init__(self, window_hours: float = 6, max_hamming: int = 3, min_jaccard: float = 0.6,
                 cross_exchange_minutes: float = 30, second_pass=None):
        self.window = timedelta(hours=window_hours)
        self.max_hamming = max_hamming
        self.min_jaccard = min_jaccard
        self.xchg = timedelta(minutes=cross_exchange_minutes)
        # Optional second pass, e.g. embedding cosine: fn(title_a, title_b) -> bool.
        self.second_pass = second_pass
        self.entries: list[Entry] = []

    def fingerprint(self, title: str, rank: int) -> tuple[int, frozenset]:
        toks = title_tokens(title, strip_publisher=rank >= 3)
        return simhash(toks), frozenset(toks)

    def assign(self, *, source: str, rank: int, title: str, symbol: str | None,
               label: str | None, at: datetime) -> tuple[int | None, str, int]:
        """Returns (cluster_id or None for a new lead, role, simhash)."""
        self.entries = [e for e in self.entries if at - e.at <= self.window]
        sh, toks = self.fingerprint(title, rank)

        if rank == 1:
            # Exchange filings: only "same company, same kind of filing, other exchange,
            # within 30 min" counts as a duplicate. Boilerplate titles must not cluster.
            if symbol:
                for e in self.entries:
                    if not (e.rank == 1 and e.source != source and e.symbol == symbol and at - e.at <= self.xchg):
                        continue
                    # Same kind of filing, or (unlabelled) headlines that mostly overlap: the NSE and BSE
                    # copies word the subject differently but share most of the content words.
                    if (label and e.label == label) or jaccard(toks, e.tokens) >= 0.35:
                        return e.cluster_id, "dupe", sh
            return None, "lead", sh

        for e in reversed(self.entries):          # newest first
            if e.rank == 1:
                continue
            if rank == 2:
                # Regulator documents are distinct by nature ("...for REITs" vs "...for InvITs"):
                # only an identical headline (same release in two feeds) is a duplicate.
                if e.rank == 2 and e.sh == sh and e.tokens == toks:
                    return e.cluster_id, "dupe", sh
                continue
            if symbol and e.symbol and symbol != e.symbol:
                continue                          # different companies are never one story
            short = min(len(toks), len(e.tokens)) < 5
            similar = (hamming(sh, e.sh) <= self.max_hamming
                       or (not short and jaccard(toks, e.tokens) >= self.min_jaccard)
                       or (self.second_pass is not None and not short and self.second_pass(title, e)))
            if similar:
                role = "followup" if e.rank <= 2 else "dupe"
                return e.cluster_id, role, sh

        if rank >= 3 and symbol:
            # Media story naming a company we already have a kept filing for.
            for e in reversed(self.entries):
                if e.rank == 1 and e.symbol == symbol and e.label:
                    return e.cluster_id, "followup", sh
        return None, "lead", sh

    def add(self, cluster_id: int, *, source: str, rank: int, title: str, symbol: str | None,
            label: str | None, at: datetime, sh: int | None = None) -> None:
        if sh is None:
            sh, toks = self.fingerprint(title, rank)
        else:
            toks = self.fingerprint(title, rank)[1]
        self.entries.append(Entry(cluster_id, at, source, rank, symbol, label, sh, toks))
