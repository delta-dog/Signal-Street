from .base import PollResult, Source, status_of
from .bse import BseSource
from .exchange_rss import BseRssSource, NseRssSource
from .nse import NseSource
from .pib import PibSource
from .rss import RssSource
from .sebi import SebiHtmlSource

SOURCE_TYPES: dict[str, type[Source]] = {
    cls.type: cls for cls in (RssSource, NseSource, BseSource, SebiHtmlSource, PibSource, NseRssSource, BseRssSource)
}

__all__ = ["SOURCE_TYPES", "PollResult", "Source", "status_of"]
