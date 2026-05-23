"""Pure-Python 1D price clustering. Ported from Shmotki price_service.
_cluster_prices. No scikit-learn dependency.

Separates a scam/parts low cluster from the genuine market cluster and
returns the median of the higher cluster, so a flood of fake "iPhone 13 for
5000" listings does not drag the learned baseline down.
"""


def _median(values: list[int]) -> int:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) // 2


def cluster_high_median(prices: list[int]) -> int | None:
    """Median of the upper (genuine) cluster via 1D 2-means."""
    prices = [p for p in prices if p > 0]
    if not prices:
        return None
    if len(prices) < 4:
        return _median(prices)

    c1, c2 = float(min(prices)), float(max(prices))
    cluster1: list[int] = prices
    cluster2: list[int] = []
    for _ in range(8):
        cluster1 = [p for p in prices if abs(p - c1) <= abs(p - c2)]
        cluster2 = [p for p in prices if abs(p - c1) > abs(p - c2)]
        if not cluster1 or not cluster2:
            break
        n1 = sum(cluster1) / len(cluster1)
        n2 = sum(cluster2) / len(cluster2)
        if n1 == c1 and n2 == c2:
            break
        c1, c2 = n1, n2

    higher = cluster1 if c1 >= c2 else cluster2
    return _median(higher) if higher else _median(prices)


def _high_cluster(prices: list[int]) -> list[int]:
    """The genuine (upper) cluster as a list, via 1D 2-means."""
    prices = [p for p in prices if p > 0]
    if len(prices) < 4:
        return prices
    c1, c2 = float(min(prices)), float(max(prices))
    cluster1, cluster2 = prices, []
    for _ in range(8):
        cluster1 = [p for p in prices if abs(p - c1) <= abs(p - c2)]
        cluster2 = [p for p in prices if abs(p - c1) > abs(p - c2)]
        if not cluster1 or not cluster2:
            break
        n1 = sum(cluster1) / len(cluster1)
        n2 = sum(cluster2) / len(cluster2)
        if n1 == c1 and n2 == c2:
            break
        c1, c2 = n1, n2
    higher = cluster1 if c1 >= c2 else cluster2
    return higher or prices


def _iqr_trim(prices: list[int], k: float = 1.5) -> list[int]:
    """Drop statistical outliers (typo / scam extremes) by the IQR rule."""
    s = sorted(prices)
    n = len(s)
    if n < 4:
        return s

    def _quantile(q: float) -> float:
        pos = (n - 1) * q
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        return s[lo] + (s[hi] - s[lo]) * frac

    q1 = _quantile(0.25)
    q3 = _quantile(0.75)
    iqr = q3 - q1
    lo, hi = q1 - k * iqr, q3 + k * iqr
    trimmed = [p for p in s if lo <= p <= hi]
    return trimmed or s


def robust_median(prices: list[int], drop_low_cluster: bool = True) -> int | None:
    """Typical price of the genuine USED bulk.

    Earlier this took the *upper* 2-means cluster, which for phones biased
    the baseline UP toward new/shop/refurb listings (e.g. 60k when the used
    market is ~43k). Now we anchor on the bulk: compute a rough median,
    drop values far below (scam/parts) and far above (new/shop bait)
    relative to it, IQR-trim the rest, and return that median.

    drop_low_cluster: True for working/ideal/good (tight band around the
    bulk); False for defect/broken/for_parts (legitimately lower & wider,
    so only IQR-trim).
    """
    prices = [p for p in prices if p > 0]
    if not prices:
        return None
    if len(prices) < 4:
        return _median(prices)
    m0 = _median(prices)
    if m0 <= 0:
        return None
    if drop_low_cluster:
        band = [p for p in prices if 0.35 * m0 <= p <= 2.5 * m0] or prices
    else:
        band = [p for p in prices if p <= 2.5 * m0] or prices
    return _median(_iqr_trim(band))
