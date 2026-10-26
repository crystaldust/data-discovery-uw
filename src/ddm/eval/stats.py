"""Proportions and their 95% intervals for a stratified, labelled sample.

- wilson: one stratum (Wilson score interval; reliable near 0 and 1, unlike the normal one).
- stratified_proportion: strata weighted by population, normal interval with the finite
  population correction (Cochran, Sampling Techniques, ch. 5). The variance uses the
  Agresti-Coull adjusted proportion (x+2)/(n+4): with the plain one, a stratum labelled
  all 'same' would claim zero error and a 100%-100% interval.
- ratio_estimate: estimated true pairs above a threshold over all estimated true pairs,
  i.e. recall. Its interval plugs each band's Wilson bounds into the ratio (lowest above
  over highest below, and the reverse): conservative and easy to explain, where a
  delta-method interval collapses to 0-100% once a large band has no positives sampled.
A stratum that has members but no labels yet makes an estimate unknown (None) rather
than silently counting as zero. All deterministic: the same labels give the same report.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

Z95 = 1.959964


@dataclass(frozen=True)
class Stratum:
    population: int
    sampled: int  # labelled same or different; 'unsure' is not counted
    positives: int  # labelled same


@dataclass(frozen=True)
class Estimate:
    point: float
    low: float
    high: float


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return (_clamp(centre - margin), _clamp(centre + margin))


def _variance_of_total(s: Stratum) -> float:
    """Variance of the estimated number of positives in one stratum (Agresti-Coull)."""
    if s.sampled == 0 or s.sampled >= s.population:
        return 0.0
    p = (s.positives + 2) / (s.sampled + 4)
    correction = 1 - s.sampled / s.population
    return s.population**2 * p * (1 - p) / (s.sampled + 4) * correction


def _incomplete(strata: Sequence[Stratum]) -> bool:
    return any(s.population > 0 and s.sampled == 0 for s in strata)


def _total(s: Stratum) -> float:
    return s.population * s.positives / s.sampled if s.sampled else 0.0


def stratified_proportion(strata: Sequence[Stratum]) -> Estimate | None:
    if _incomplete(strata):
        return None
    labelled = [s for s in strata if s.sampled > 0]
    population = sum(s.population for s in labelled)
    if population == 0:
        return None
    point = sum(_total(s) for s in labelled) / population
    margin = Z95 * math.sqrt(sum(_variance_of_total(s) for s in labelled)) / population
    return Estimate(point, _clamp(point - margin), _clamp(point + margin))


def _wilson_totals(strata: Sequence[Stratum]) -> tuple[float, float]:
    low = high = 0.0
    for s in strata:
        if s.sampled == 0:
            continue
        if s.sampled >= s.population:  # a census has no sampling error
            low += s.positives
            high += s.positives
            continue
        p_low, p_high = wilson(s.positives, s.sampled)
        low += s.population * p_low
        high += s.population * p_high
    return low, high


def _share(part: float, rest: float) -> float:
    return part / (part + rest) if part + rest > 0 else 0.0


def ratio_estimate(above: Sequence[Stratum], below: Sequence[Stratum]) -> Estimate | None:
    if _incomplete([*above, *below]):
        return None
    a = sum(_total(s) for s in above)
    b = sum(_total(s) for s in below)
    if a + b == 0:
        return None
    a_low, a_high = _wilson_totals(above)
    b_low, b_high = _wilson_totals(below)
    return Estimate(a / (a + b), _share(a_low, b_high), _share(a_high, b_low))
