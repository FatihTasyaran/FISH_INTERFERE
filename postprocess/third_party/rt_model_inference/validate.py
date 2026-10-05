"""Validation helpers for inferred models."""

import functools
from collections.abc import Iterator, Sequence

from .certain_periodic import PeriodicModel
from .certain_sporadic import max_releases, min_releases
from .iterators import is_empty
from .time import EPSILON, Duration, Instant, ReleaseWindow


def find_closed_interval_containing(
    releases: Sequence[Instant], target_delta: Duration, target_count: int
) -> tuple[int, int] | None:
    """Find a closed interval of a given length containing an exact given
    number of releases, with releases occurring at both end points."""

    gaps = (
        (x, y)
        for x, y in zip(releases, releases[target_count - 1 :])
        if y - x + EPSILON == target_delta
    )
    try:
        return next(gaps)
    except StopIteration:
        return None


def dmin_curve_is_tight(dmin: list[Duration], releases: Sequence[Instant]) -> bool:
    "Check whether the given delta-min vector is tight for the given release sequence."
    if not releases:
        return True
    for n, delta in enumerate(dmin):
        if n == 0 and delta != 0:
            return False
        elif n == 1 and delta != 1:
            return False
        elif n > 1:
            witness = find_closed_interval_containing(releases, delta, n)
            if witness is None:
                return False
    return True


def max_open_interval_containing(
    releases: Sequence[Instant], target_count: int
) -> tuple[Duration, Instant, Instant]:
    """Find the shortest open interval containing an exact given
    number of releases, with releases occurring at both non-included end points."""

    gaps = (
        (y - x - EPSILON, x, y) for x, y in zip(releases, releases[target_count + 1 :])
    )

    return max(gaps, default=(0, 0, 0))


def max_interval_containing(
    releases: Sequence[Instant], target_count: int
) -> tuple[Duration, Instant, Instant]:
    """Find the shortest interval containing an exact given number of releases."""
    open = max_open_interval_containing(releases, target_count)
    if target_count < len(releases):
        x = 0
        y = target_count
        start = (releases[y] - releases[x], x, y)
        y = len(releases) - 1
        x = len(releases) - 1 - target_count
        end = (releases[y] - releases[x], x, y)
    else:
        x = 0
        y = -1
        start = end = (releases[y] - releases[x] + EPSILON, x, y)
    return max(open, start, end)


def dmax_curve_is_tight(dmax: list[Duration], releases: Sequence[Instant]) -> bool:
    "Check whether the given delta-max vector is tight for the given release sequence."

    if not releases:
        return True
    for n, delta in enumerate(dmax):
        longest = max_interval_containing(releases, n)
        if longest[0] != delta:
            return False
    return True


def test_intervals(
    releases: Sequence[Instant],
    max_delta: Duration,
    max_count: int,
) -> Iterator[tuple[tuple[Instant, Instant], int]]:
    first_release = releases[0]
    last_release = releases[-1]
    tested: set[tuple[Instant, Instant]] = set()

    INTERVAL_EDGE_ADJUSTMENTS = (
        (0, 0),
        (1, 0),
        (0, 1),
        (-1, 0),
        (0, -1),
        (1, -1),
        (-1, 1),
    )

    start = 0
    for end in range(len(releases)):
        while releases[end] - releases[start] > max_delta or end - start > max_count:
            start += 1
        for x in range(start, end):
            y = end
            for dx, dy in INTERVAL_EDGE_ADJUSTMENTS:
                interval = (releases[x] + dx, releases[y] + dy)
                if (
                    interval[0] <= interval[-1]
                    and interval[0] >= first_release
                    and interval[1] <= last_release
                    and interval[1] - interval[0] < max_delta
                    and interval not in tested
                ):
                    # move x to the first release included in the interval
                    while x < len(releases) and releases[x] < interval[0]:
                        x += 1
                    while x > 0 and releases[x - 1] >= interval[0]:
                        x -= 1
                    # move y to the last release included in the interval
                    while y >= 0 and releases[y] > interval[1]:
                        y -= 1
                    while (y + 1) < len(releases) and releases[y + 1] <= interval[1]:
                        y += 1

                    if y < x:
                        count = 0
                    else:
                        count = y - x + 1

                    yield (interval, count)
                    tested.add(interval)


def dmin_counterexamples(
    dmin: list[Duration], releases: Sequence[Instant]
) -> Iterator[tuple[Instant, Instant]]:
    """Yield windows in which the number of releases exceeds the dmin curve."""
    bound = functools.cache(functools.partial(max_releases, dmin))
    for (x, y), observed_count in test_intervals(
        releases, dmin[-1] - EPSILON if len(dmin) > 0 else 0, len(dmin) + 1
    ):
        delta = y - x + EPSILON
        upper = bound(delta)
        if upper is None or observed_count > upper:
            yield (x, y)


def dmin_curve_upper_bounds_releases(
    dmin: list[Duration], releases: Sequence[Instant]
) -> bool:
    """Check whether a dmin curve upper-bounds all covered windows."""
    return is_empty(dmin_counterexamples(dmin, releases))


def dmax_counterexamples(
    dmax: list[Duration], releases: Sequence[Instant]
) -> Iterator[tuple[Instant, Instant]]:
    """Yield windows in which the number of releases is below the dmax curve."""
    bound = functools.cache(functools.partial(min_releases, dmax))
    for (x, y), observed_count in test_intervals(
        releases, dmax[-1] if len(dmax) > 0 else 0, len(dmax) + 1
    ):
        delta = y - x + EPSILON
        lower = bound(delta)
        if lower is None or observed_count < lower:
            yield (x, y)


def dmax_curve_lower_bounds_releases(
    dmax: list[Duration], releases: Sequence[Instant]
) -> bool:
    """Check whether a dmax curve lower-bounds all covered windows."""
    return is_empty(dmax_counterexamples(dmax, releases))


def inexplicable_releases(
    pm: PeriodicModel, releases: Sequence[Instant]
) -> Iterator[tuple[int, Instant]]:
    """Yield releases not covered by the periodic model."""
    for i, rel in enumerate(releases):
        arrival = pm.offset + i * pm.period
        if not (arrival <= rel <= arrival + pm.jitter):
            yield i, rel


def model_is_conservative(pm: PeriodicModel, releases: Sequence[Instant]) -> bool:
    """Check whether a periodic model covers every exact release."""
    return is_empty(inexplicable_releases(pm, releases))


def uncovered_windows(
    pm: PeriodicModel, windows: Sequence[ReleaseWindow]
) -> Iterator[tuple[int, ReleaseWindow]]:
    """Yield release windows not fully covered by the periodic model."""
    for i, win in enumerate(windows):
        arrival = pm.offset + i * pm.period
        if not (arrival <= win[0] and win[1] <= arrival + pm.jitter):
            yield i, win


def model_covers_all(pm: PeriodicModel, windows: Sequence[ReleaseWindow]) -> bool:
    """Check whether a periodic model fully covers all release windows."""
    return is_empty(uncovered_windows(pm, windows))


def disjoint_windows(
    pm: PeriodicModel, windows: Sequence[ReleaseWindow]
) -> Iterator[tuple[int, ReleaseWindow]]:
    """Yield release windows that do not intersect the periodic model."""
    for i, win in enumerate(windows):
        arrival = pm.offset + i * pm.period
        if arrival > win[1] or arrival + pm.jitter < win[0]:
            yield i, win


def model_intersects_all(pm: PeriodicModel, windows: Sequence[ReleaseWindow]) -> bool:
    """Check whether a periodic model intersects all release windows."""
    return is_empty(disjoint_windows(pm, windows))
