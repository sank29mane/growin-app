"""Decimal performance and significance metrics (D-18, RESEARCH Q3).

New functions on purpose: the float ``portfolio_analyzer`` helpers are not reused
(float Sharpe, UK risk-free rate). The normal CDF and its inverse are built from
Decimal series and bisection so no float enters this package outside
``regime.py``. The bootstrap uses a seeded integer generator, never ``random``.
"""

from __future__ import annotations

import decimal
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal(0)
ONE = Decimal(1)
_CTX = decimal.Context(prec=60, rounding=decimal.ROUND_HALF_EVEN)
_PI = Decimal("3.14159265358979323846264338327950288419716939937510582097494459230781640628620899")
_EULER_GAMMA = Decimal("0.5772156649015328606065120900824024310421593359")
SESSIONS_PER_YEAR = 250
_MASK = (1 << 64) - 1


def net_return(start: Decimal, end: Decimal) -> Decimal:
    if start <= 0:
        raise ValueError("starting equity must be positive")
    return end / start - ONE


def max_drawdown(curve: Sequence[Decimal]) -> Decimal:
    """Peak-to-trough on the marked equity curve. Zero or negative."""
    worst, peak = ZERO, None
    for value in curve:
        peak = value if peak is None or value > peak else peak
        dd = value / peak - ONE
        if dd < worst:
            worst = dd
    return worst


def trailing_drawdown(curve: Sequence[Decimal]) -> Decimal:
    """Drawdown of the last point from the running peak."""
    if not curve:
        return ZERO
    return curve[-1] / max(curve) - ONE


def daily_returns(curve: Sequence[Decimal]) -> list[Decimal]:
    return [curve[i] / curve[i - 1] - ONE for i in range(1, len(curve))]


def mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, ZERO) / len(values)


def stdev(values: Sequence[Decimal]) -> Decimal:
    """Sample standard deviation."""
    if len(values) < 2:
        return ZERO
    with decimal.localcontext(_CTX):
        m = mean(values)
        return (sum(((v - m) ** 2 for v in values), ZERO) / (len(values) - 1)).sqrt()


def skew_kurtosis(values: Sequence[Decimal]) -> tuple[Decimal, Decimal]:
    """Population skewness and raw kurtosis. (0, 3) for a degenerate series."""
    n = len(values)
    if n < 4:
        return ZERO, Decimal(3)
    with decimal.localcontext(_CTX):
        m = mean(values)
        m2 = sum(((v - m) ** 2 for v in values), ZERO) / n
        if m2 <= 0:
            return ZERO, Decimal(3)
        m3 = sum(((v - m) ** 3 for v in values), ZERO) / n
        m4 = sum(((v - m) ** 4 for v in values), ZERO) / n
        return m3 / (m2 * m2.sqrt()), m4 / (m2 * m2)


def sharpe_per_bar(returns: Sequence[Decimal]) -> Decimal | None:
    sd = stdev(returns)
    if sd <= 0:
        return None
    return mean(returns) / sd


def annualise_sharpe(per_bar: Decimal) -> Decimal:
    with decimal.localcontext(_CTX):
        return per_bar * Decimal(SESSIONS_PER_YEAR).sqrt()


# ---- normal distribution in Decimal --------------------------------------------
def _erf(x: Decimal) -> Decimal:
    if x == 0:
        return ZERO
    with decimal.localcontext(_CTX):
        term = x
        total = x
        n = 0
        x2 = x * x
        while True:
            n += 1
            term = -term * x2 / n
            add = term / (2 * n + 1)
            total += add
            if abs(add) < Decimal("1e-45"):
                break
        return 2 / _PI.sqrt() * total


def norm_cdf(x: Decimal) -> Decimal:
    with decimal.localcontext(_CTX):
        if x > 9:
            return ONE
        if x < -9:
            return ZERO
        return (ONE + _erf(x / Decimal(2).sqrt())) / 2


def norm_ppf(p: Decimal) -> Decimal:
    if not ZERO < p < ONE:
        raise ValueError("probability must lie strictly between 0 and 1")
    with decimal.localcontext(_CTX):
        lo, hi = Decimal(-9), Decimal(9)
        for _ in range(110):
            mid = (lo + hi) / 2
            if norm_cdf(mid) < p:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2


def _variance_term(sr: Decimal, skew: Decimal, kurt: Decimal) -> Decimal:
    return ONE - skew * sr + (kurt - ONE) / 4 * sr * sr


def psr(sr: Decimal, benchmark_sr: Decimal, n: int, skew: Decimal, kurt: Decimal) -> Decimal | None:
    """Probabilistic Sharpe ratio (per-bar Sharpe values). None when the variance term is not positive."""
    if n < 2:
        return None
    with decimal.localcontext(_CTX):
        term = _variance_term(sr, skew, kurt)
        if term <= 0:
            return None
        return norm_cdf((sr - benchmark_sr) * Decimal(n - 1).sqrt() / term.sqrt())


def min_track_record_length(sr: Decimal, benchmark_sr: Decimal, skew: Decimal, kurt: Decimal, confidence: Decimal) -> Decimal | None:
    """MinTRL in bars. None when SR does not exceed the benchmark."""
    if sr <= benchmark_sr:
        return None
    with decimal.localcontext(_CTX):
        term = _variance_term(sr, skew, kurt)
        if term <= 0:
            return None
        z = norm_ppf(confidence)
        return ONE + term * (z / (sr - benchmark_sr)) ** 2


def expected_max_sharpe(trials: int, trial_sd: Decimal) -> Decimal:
    """Expected maximum per-bar Sharpe of ``trials`` unskilled configurations with Sharpe spread ``trial_sd``."""
    if trials < 2:
        return ZERO
    with decimal.localcontext(_CTX):
        e = ONE.exp()
        n = Decimal(trials)
        return trial_sd * ((ONE - _EULER_GAMMA) * norm_ppf(ONE - ONE / n) + _EULER_GAMMA * norm_ppf(ONE - ONE / (n * e)))


def dsr(sr: Decimal, n: int, skew: Decimal, kurt: Decimal, trials: int, trial_sd_annual: Decimal) -> Decimal | None:
    """Deflated Sharpe ratio: PSR against the expected best of ``trials`` unskilled trials."""
    with decimal.localcontext(_CTX):
        per_bar_sd = trial_sd_annual / Decimal(SESSIONS_PER_YEAR).sqrt()
        return psr(sr, expected_max_sharpe(trials, per_bar_sd), n, skew, kurt)


# ---- seeded integer PRNG and stationary block bootstrap -------------------------
class SplitMix64:
    def __init__(self, seed: int) -> None:
        self._state = seed & _MASK

    def next(self) -> int:
        self._state = (self._state + 0x9E3779B97F4A7C15) & _MASK
        z = self._state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
        return z ^ (z >> 31)


def stationary_bootstrap_interval(
    values: Sequence[Decimal], *, seed: int, resamples: int, mean_block: int, lower: Decimal, upper: Decimal
) -> tuple[Decimal, Decimal] | None:
    """Percentile interval of the SUM of the series under a stationary block bootstrap (Politis and Romano)."""
    n = len(values)
    if n < 2 or resamples < 20:
        return None
    rng = SplitMix64(seed)
    restart = (1 << 64) // max(1, mean_block)
    sums: list[Decimal] = []
    for _ in range(resamples):
        total = ZERO
        i = rng.next() % n
        for _ in range(n):
            total += values[i]
            i = rng.next() % n if rng.next() < restart else (i + 1) % n
        sums.append(total)
    sums.sort()

    def pick(q: Decimal) -> Decimal:
        index = int((q * (len(sums) - 1)).to_integral_value(rounding=decimal.ROUND_HALF_EVEN))
        return sums[index]

    return pick(lower), pick(upper)


# ---- trade and turnover statistics -------------------------------------------------
def hit_rate(net_returns: Sequence[Decimal]) -> Decimal | None:
    if not net_returns:
        return None
    return Decimal(sum(1 for r in net_returns if r > 0)) / len(net_returns)


def swaps_per_week(swaps: int, sessions: int) -> Decimal:
    if sessions <= 0:
        raise ValueError("sessions must be positive")
    return Decimal(swaps) * 5 / sessions


@dataclass(frozen=True)
class SignificanceSummary:
    per_bar_sharpe: Decimal | None
    annual_sharpe: Decimal | None
    psr_vs_zero: Decimal | None
    min_trl_bars: Decimal | None
    dsr: Decimal | None
    trials: int
    bars: int


def significance(returns: Sequence[Decimal], *, trials: int, trial_sd_annual: Decimal, confidence: Decimal) -> SignificanceSummary:
    """PSR against zero, MinTRL at ``confidence`` and DSR with the registered trial count."""
    n = len(returns)
    sr = sharpe_per_bar(returns) if n >= 2 else None
    if sr is None:
        return SignificanceSummary(None, None, None, None, None, trials, n)
    skew, kurt = skew_kurtosis(returns)
    return SignificanceSummary(
        per_bar_sharpe=sr,
        annual_sharpe=annualise_sharpe(sr),
        psr_vs_zero=psr(sr, ZERO, n, skew, kurt),
        min_trl_bars=min_track_record_length(sr, ZERO, skew, kurt, confidence),
        dsr=dsr(sr, n, skew, kurt, trials, trial_sd_annual),
        trials=trials,
        bars=n,
    )
