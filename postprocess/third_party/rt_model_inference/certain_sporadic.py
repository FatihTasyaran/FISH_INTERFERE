"""Inference of delta-min and delta-max curves from sequences of certain release times."""

from collections import deque
from collections.abc import Iterable

from .time import EPSILON, Duration, Instant


def infer_delta_min(
    releases: Iterable[Instant], nmax: int | None = None
) -> list[Duration]:
    """Given a sequence of release times, infer a delta-min vector representing
    the given release times.

    Returns a vector `v` such that `v[n]` indicates the length of the shortest
    interval in which at least `n` releases were observed.

    If `nmax` is provided, a delta-min prefix of length at most `max(2, nmax)` is
    returned (`nmax` must be at least 2).
    """

    if nmax is not None and nmax <= 1:
        raise ValueError("nmax must be at least 2")

    dmin: list[Duration] = [0, 1]
    buffer: deque[Instant] = deque() if nmax is None else deque(maxlen=nmax - 1)

    for i, rel_i in enumerate(releases):
        if len(buffer) != 0 and buffer[-1] > rel_i:
            raise ValueError("releases must be monotonic")
        for j, rel_j in enumerate(buffer, start=(i - len(buffer))):
            count = i - j + 1
            delta = rel_i - rel_j + EPSILON
            if len(dmin) == count:
                dmin.append(delta)
            else:
                dmin[count] = min(dmin[count], delta)
        buffer.append(rel_i)

    if len(buffer) == 0:
        # we didn't see any releases
        return []
    else:
        return dmin


def infer_sporadic_model(releases: Iterable[Instant]) -> Duration:
    """Given a sequence of release times, infer a sporadic task model.

    Returns the minimum observed separation between any two releases.
    """

    releases = iter(releases)

    try:
        last = next(releases)
        r = next(releases)
        min_sep = r - last
        last = r
        if min_sep < 0:
            raise ValueError("releases must be monotonic")
    except StopIteration:
        raise ValueError("need at least two releases to infer a sporadic task model")

    for r in releases:
        min_sep = min(min_sep, r - last)
        if min_sep < 0:
            raise ValueError("releases must be monotonic")
        last = r

    return min_sep


def max_releases(delta_min: list[Duration], delta: Duration) -> int | None:
    """Given a delta-min vector and a `delta`, return the maximum number of releases
    in any interval of length `delta`.

    Returns `None` if the given delta-min vector does not cover the given `delta`.
    """

    if not delta_min:
        return None

    if delta >= delta_min[-1]:
        # interval too large, we have no information
        return None

    for n, dmin in enumerate(delta_min[1:]):
        if dmin > delta:
            return n

    assert False, "unreachable"


def infer_delta_max(
    releases: Iterable[Instant], nmax: int | None = None
) -> list[Duration]:
    """Given a sequence of release times, infer a delta-max vector representing
    the given release times.

    Returns a vector `v` such that `v[n]` indicates the length of the longest
    interval in which at most `n` releases were observed.

    If `nmax` is provided, a delta-max prefix of length at most `max(2, nmax)` is
    returned (`nmax` must be positive).
    """

    if nmax is not None and nmax <= 0:
        raise ValueError("nmax must be positive")

    dmax: list[Duration] = []
    buffer: deque[Instant] = deque() if nmax is None else deque(maxlen=nmax + 1)

    def update(rel_i: Instant):
        for j, rel_j in enumerate(buffer):
            count = len(buffer) - j - 1
            if nmax is None or count <= nmax:
                delta = max(0, rel_i - rel_j - EPSILON)
                if len(dmax) == count:
                    dmax.append(delta)
                else:
                    dmax[count] = max(dmax[count], delta)

    for rel_i in releases:
        if len(buffer) != 0:
            if buffer[-1] > rel_i:
                raise ValueError("releases must be monotonic")
        else:
            # To deal with the special case of the first sample, we
            # insert a "dummy" sample into the moving window just
            # before the first sample.
            buffer.append(rel_i - EPSILON)

        update(rel_i)
        buffer.append(rel_i)

    # To deal with the special case of the last sample, we process
    # a "dummy" sample just after the last sample.
    if len(buffer) > 0:
        update(buffer[-1] + EPSILON)

    return dmax


def min_releases(delta_max: list[Duration], delta: Duration) -> int | None:
    """Given a delta-max vector and a `delta`, return the minimum number of releases
    in any interval of length `delta`.

    Returns `None` if the given delta-max vector does not cover the given `delta`.
    """

    if not delta_max:
        return None

    if delta > delta_max[-1]:
        # interval too large, we have no information
        return None

    for n, dmax in enumerate(delta_max):
        if dmax >= delta:
            return n

    assert False, "unreachable"
