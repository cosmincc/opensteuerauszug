"""Stock splits that Trading212 exports do not contain.

Trading212 only started exporting ``Stock split close`` / ``Stock split open``
rows around December 2024; earlier splits often have no row at all (neither
the pair nor the older single ``Stock Split`` row).  The API order history
never contains splits.  Without them, pre-split purchases are counted in old
shares and later trades in new shares, which silently corrupts reconstructed
holdings.

The table lists well-known forward splits before December 2024 (date = first
trading day at the new count).  It follows the list documented by
KapJI/capital-gains-calculator (MIT licensed), docs/brokers/trading212.md.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import FrozenSet


@dataclass(frozen=True)
class KnownSplit:
    name: str
    isins: FrozenSet[str]
    effective: date  # first trading day at the new count
    ratio: Decimal  # new shares per old share


def _split(name: str, isins: str, effective: date, ratio: int) -> KnownSplit:
    return KnownSplit(name, frozenset(isins.split()), effective, Decimal(ratio))


KNOWN_SPLITS: tuple[KnownSplit, ...] = (
    _split("Apple", "US0378331005", date(2020, 8, 31), 4),
    _split("Tesla", "US88160R1014", date(2020, 8, 31), 5),
    _split("NVIDIA", "US67066G1040", date(2021, 7, 20), 4),
    _split("Amazon", "US0231351067", date(2022, 6, 6), 20),
    _split("DexCom", "US2521311074", date(2022, 6, 13), 4),
    _split("Shopify", "CA82509L1076", date(2022, 6, 29), 10),
    _split("Alphabet Class A", "US02079K3059", date(2022, 7, 18), 20),
    _split("Alphabet Class C", "US02079K1079", date(2022, 7, 18), 20),
    _split("Tesla", "US88160R1014", date(2022, 8, 25), 3),
    _split("Palo Alto Networks", "US6974351057", date(2022, 9, 14), 3),
    _split("Novo Nordisk ADR", "US6701002056", date(2023, 9, 20), 2),
    _split("Walmart", "US9311421039", date(2024, 2, 26), 3),
    _split("NVIDIA", "US67066G1040", date(2024, 6, 10), 10),
    _split("Chipotle Mexican Grill", "US1696561059", date(2024, 6, 26), 50),
    _split("Broadcom", "US11135F1012", date(2024, 7, 15), 10),
    _split("MicroStrategy", "US5949724083", date(2024, 8, 8), 10),
    _split("Cintas", "US1729081059", date(2024, 9, 12), 4),
    _split("Deckers Outdoor", "US2435371073", date(2024, 9, 17), 6),
    _split("Super Micro Computer", "US86800U1043 US86800U3023", date(2024, 10, 1), 10),
    _split("Lam Research", "US5128071082 US5128073062", date(2024, 10, 3), 10),
)
