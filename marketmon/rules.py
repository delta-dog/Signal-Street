"""Rules-first noise filter and priority scoring. No network, no LLM, always on."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Item

P0, P1, P2, P3 = 0, 1, 2, 3   # instant, digest, log only, dropped noise


@dataclass
class Constituent:
    symbol: str
    name: str
    isin: str | None = None
    bse_code: str | None = None
    heavyweight: bool = False
    aliases: tuple[str, ...] = ()


@dataclass
class Classification:
    symbol: str | None = None        # matched index constituent
    heavyweight: bool = False
    label: str | None = None         # first matching keep label
    priority: int = P2
    drop_reason: str | None = None
    impact: str | None = None        # heavy / stock / index / None (can't move the Nifty or Sensex)


def norm_company(name: str) -> str:
    s = re.sub(r"[^a-z0-9&]+", " ", (name or "").lower())
    return " ".join(w for w in s.split() if w not in {"ltd", "limited", "the", "co", "company"})


class Index:
    """Nifty 50 / Sensex allowlist with symbol, BSE code, ISIN and headline-alias lookup."""

    def __init__(self, constituents: list[dict]):
        self.items = [Constituent(
            symbol=c["symbol"], name=c["name"], isin=c.get("isin"),
            bse_code=str(c["bse_code"]) if c.get("bse_code") else None,
            heavyweight=bool(c.get("heavyweight")), aliases=tuple(c.get("aliases", [])),
        ) for c in constituents]
        self.by_symbol = {c.symbol: c for c in self.items}
        self.by_bse = {c.bse_code: c for c in self.items if c.bse_code}
        self.by_isin = {c.isin: c for c in self.items if c.isin}
        self.by_name = {norm_company(c.name): c for c in self.items}
        # Aliases match case-sensitively ("Eternal" the company, not "eternal" the word).
        self.alias_re = [(re.compile(rf"(?<![\w&]){re.escape(a)}(?![\w&])"), c)
                         for c in self.items for a in c.aliases]

    def match(self, item: Item) -> Constituent | None:
        if item.isin and item.isin in self.by_isin:
            return self.by_isin[item.isin]
        if item.source_rank == 1:
            # Any exchange feed (JSON API or RSS): NSE symbol, BSE scrip code, then company name.
            sym = item.symbol or ""
            return (self.by_symbol.get(sym) or (self.by_bse.get(sym) if sym.isdigit() else None)
                    or self.by_name.get(norm_company(item.entity or "")))
        title = item.title or ""
        upper = title.isupper()
        for rx, c in self.alias_re:
            if rx.search(title) or (upper and re.search(rx.pattern.upper(), title)):
                return c
        return None


def _rx(p: str) -> re.Pattern:
    return re.compile(p, re.I)


def profile(item: Item) -> str:
    """Which rule set applies: exchange, rbi, sebi, pib, fed or media."""
    if item.source_rank == 1:
        return "exchange"
    for p in ("rbi", "sebi", "pib", "fed"):
        if item.source.startswith(p):
            return p
    return "media"


class Rules:
    def __init__(self, cfg: dict, index: Index):
        self.index = index
        self.drop = [_rx(p) for p in cfg.get("drop", [])]
        self.keep = [(label, _rx(p)) for label, p in cfg.get("keep", {}).items()]
        self.p0_labels = set(cfg.get("p0_labels_constituent", ["results", "board_outcome"]))
        self.rbi, self.sebi, self.pib, self.fed = (cfg.get(k, {}) for k in ("rbi", "sebi", "pib", "fed"))
        imp = cfg.get("index_impact", {})
        self.macro = _rx(imp.get("macro", "$^"))
        self.not_news = _rx(imp.get("exclude", "$^"))

    def index_wide(self, text: str) -> bool:
        """Headline about an index-wide driver (and not just coverage of the market's moves)."""
        return bool(self.macro.search(text)) and not self.not_news.search(text)

    def classify(self, item: Item) -> Classification:
        text = f"{item.title} {item.category or ''}"
        c = Classification()
        con = self.index.match(item)
        if con:
            c.symbol, c.heavyweight = con.symbol, con.heavyweight
        c.label = next((label for label, rx in self.keep if rx.search(text)), None)
        prof = profile(item)

        # Routine-filing noise applies to company news only, never to regulator output.
        if prof in ("exchange", "media"):
            hit = next((rx for rx in self.drop if rx.search(text)), None)
            if hit and c.label not in ("fraud_default", "auditor", "regulatory"):
                c.priority, c.drop_reason, c.label = P3, hit.pattern[:60], None
                return c

        # Impact uses the headline only (a Google News query or feed category is not news).
        # A media mention of a Nifty name counts only with a market-moving label.
        stock_news = c.symbol and (prof != "media" or c.label)
        c.impact = ("heavy" if stock_news and c.heavyweight else "stock" if stock_news
                    else "index" if self.index_wide(item.title) or (prof != "media" and self._policy(prof, text)) else None)
        if prof == "rbi" and _rx(self.rbi.get("not_index", "$^")).search(text) and not c.symbol:
            c.impact = None
        c.priority = getattr(self, f"_{prof}")(item, c, text)
        if c.impact is None:
            c.priority = max(c.priority, P2)     # can't move the Nifty/Sensex -> never alerted
        return c

    def _policy(self, prof: str, text: str) -> bool:
        """Regulator items that are index-wide by nature (RBI rates/prudential, Cabinet energy/finance)."""
        if prof == "rbi":
            return bool(_rx(self.rbi.get("p0", "$^")).search(text) or _rx(self.rbi.get("p1", "$^")).search(text))
        if prof == "sebi":
            return bool(_rx(self.sebi.get("p1", "$^")).search(text))
        if prof == "fed":
            return bool(_rx(self.fed.get("p1", "$^")).search(text))
        return False

    def _exchange(self, item, c, text):
        if not c.symbol:
            return P2
        if c.label and (c.heavyweight or c.label in self.p0_labels):
            return P0
        return P1 if c.label else P2

    def _rbi(self, item, c, text):
        if _rx(self.rbi.get("p0", "$^")).search(text):
            return P1 if _rx(self.rbi.get("p0_exclude", "$^")).search(text) else P0
        if c.symbol or _rx(self.rbi.get("p1", "$^")).search(text) or self.index_wide(item.title):
            return P1
        return P2

    def _sebi(self, item, c, text):
        if c.heavyweight:
            return P0
        return P1 if c.symbol or c.impact == "index" else P2

    def _pib(self, item, c, text):
        ministry = item.entity or item.category or ""
        if any(m.lower() in ministry.lower() for m in self.pib.get("p0_ministries", [])) \
                and _rx(self.pib.get("p0", "$^")).search(item.title):
            c.impact = c.impact or "index"
            return P0
        return P1 if c.impact else P2

    def _fed(self, item, c, text):
        return P1 if _rx(self.fed.get("p1", "$^")).search(text) else P2

    def _media(self, item, c, text):
        return P1 if c.symbol and c.label else P2
