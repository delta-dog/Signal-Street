from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from marketmon.models import TIER_RANK, Item
from marketmon.rules import Index, Rules

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
T0 = datetime(2026, 11, 16, 7, 0, tzinfo=timezone.utc)   # 12:30 IST, a trading day


def fixture_bytes(name: str) -> bytes:
    return (FIX / name).read_bytes()


def make_item(source="nse", title="X", *, tier="exchange", id=None, symbol=None, isin=None,
              entity=None, category=None, published=T0, seen=None, url="https://example.com/a") -> Item:
    return Item(source=source, source_rank=TIER_RANK[tier], id=id or f"{source}:{title}", title=title,
                url=url, published_at=published, first_seen_at=seen or (published + timedelta(seconds=12)),
                symbol=symbol, isin=isin, entity=entity, category=category)


@pytest.fixture(scope="session")
def rules() -> Rules:
    idx = yaml.safe_load((ROOT / "config/index.yaml").read_text(encoding="utf-8"))
    cfg = yaml.safe_load((ROOT / "config/rules.yaml").read_text(encoding="utf-8"))
    return Rules(cfg, Index(idx["constituents"]))
