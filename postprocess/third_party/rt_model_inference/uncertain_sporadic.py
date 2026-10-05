"""Inference of delta-min-{hi,lo} and delta-max-{hi,lo} curves from sequences of
uncertain release-time windows."""

from collections import deque
from collections.abc import Iterable

from rt_model_inference.iterators import is_monotonic

from .time import EPSILON, Duration, ReleaseWindow


def infer_delta_min_hi(
    release_windows: Iterable[ReleaseWindow], nmax: int | None = None
) -> list[Duration]:
    """Given a sequence of release-time windows, infer a delta-min-hi vector
    that under-approximates the minimal separation between releases.

    Returns a vector `v` such that `v[n]` indicates a lower bound on the shortest
    interval in which at least `n` releases were observed.

    If `nmax` is provided, a delta-min-hi prefix of length at most `max(2, nmax)` is
    returned (`nmax` must be at least 2).
    """

    if nmax is not None and nmax <= 1:
        raise ValueError("nmax must be at least 2")

    dmin: list[int] = [0, 1]
    buffer: deque[ReleaseWindow] = deque() if nmax is None else deque(maxlen=nmax - 1)

    for i, rel_win in enumerate(release_windows):
        rel_i_lo = rel_win[0]
        if len(buffer) != 0:
            if buffer[-1][0] > rel_win[0]:
                raise ValueError("release-window lower bounds must be monotonic")
            if buffer[-1][1] > rel_win[1]:
                raise ValueError("release-window upper bounds must be monotonic")
        for j, rel_win_j in enumerate(buffer, start=(i - len(buffer))):
            rel_j_hi = rel_win_j[1]  # use the upper bound on the prior release time
            count = i - j + 1
            # Use the lower bound on the current release time.
            delta = max(EPSILON, rel_i_lo - rel_j_hi + EPSILON)
            if len(dmin) == count:
                dmin.append(delta)
            else:
                dmin[count] = min(dmin[count], delta)

        buffer.append(rel_win)

    if len(buffer) == 0:
        # we didn't see any releases
        return []
    else:
        assert is_monotonic(dmin)
        return dmin


def infer_delta_min_lo(
    release_windows: Iterable[ReleaseWindow], nmax: int | None = None
) -> list[Duration]:
    """Given a sequence of release-time windows, infer a delta-min-lo vector
    that over-approximates the minimal separation between releases.

    Returns a vector `v` such that `v[n]` indicates an upper bound on the shortest
    interval in which at least `n` releases were observed.

    If `nmax` is provided, a delta-min-lo prefix of length at most `max(2, nmax)` is
    returned (`nmax` must be at least 2).
    """

    if nmax is not None and nmax <= 1:
        raise ValueError("nmax must be at least 2")

    dmin: list[int] = [0, 1]
    buffer: deque[ReleaseWindow] = deque() if nmax is None else deque(maxlen=nmax - 1)

    for i, rel_win in enumerate(release_windows):
        rel_i_hi = rel_win[1]
        if len(buffer) != 0:
            if buffer[-1][0] > rel_win[0]:
                raise ValueError("release-window lower bounds must be monotonic")
            if buffer[-1][1] > rel_win[1]:
                raise ValueError("release-window upper bounds must be monotonic")
        for j, rel_win_j in enumerate(buffer, start=(i - len(buffer))):
            rel_j_lo = rel_win_j[0]  # use the lower bound on the prior release time
            count = i - j + 1
            # Use the upper bound on the current release time.
            delta = max(EPSILON, rel_i_hi - rel_j_lo + EPSILON)
            if len(dmin) == count:
                dmin.append(delta)
            else:
                dmin[count] = min(dmin[count], delta)

        buffer.append(rel_win)

    if len(buffer) == 0:
        # we didn't see any releases
        return []
    else:
        assert is_monotonic(dmin)
        return dmin


def infer_delta_max_lo(
    release_windows: Iterable[ReleaseWindow], nmax: int | None = None
) -> list[Duration]:
    """Given a sequence of release-time windows, infer a delta-max-lo vector
    that over-approximates the maximal exact-count intervals.

    Returns a vector `v` such that `v[n]` indicates an upper bound on the
    longest interval in which at most `n` releases were observed.

    If `nmax` is provided, a delta-max-lo prefix of length at most `nmax + 1` is
    returned (`nmax` must be positive).
    """

    if nmax is not None and nmax <= 0:
        raise ValueError("nmax must be positive")

    dmax: list[Duration] = []
    buffer: deque[ReleaseWindow] = deque() if nmax is None else deque(maxlen=nmax + 1)

    def update(rel_win: ReleaseWindow):
        # for over-approximation, use upper bound on current release time
        rel_i_hi = rel_win[1]

        for j, rel_win_j in enumerate(buffer):
            # for over-approximation of distance, use lower bound on prior release time
            rel_j_lo = rel_win_j[0]
            count = len(buffer) - j - 1
            if nmax is None or count <= nmax:
                delta = max(0, rel_i_hi - rel_j_lo - EPSILON)
                if len(dmax) == count:
                    dmax.append(delta)
                else:
                    dmax[count] = max(dmax[count], delta)

    for rel_win in release_windows:
        if len(buffer) != 0:
            if buffer[-1][0] > rel_win[0]:
                raise ValueError("release-window lower bounds must be monotonic")
            if buffer[-1][1] > rel_win[1]:
                raise ValueError("release-window upper bounds must be monotonic")
        else:
            # To deal with the special case of the first sample, we insert a
            # "dummy" sample into the buffer just before the first sample.
            dummy = (rel_win[0] - EPSILON, rel_win[1])
            buffer.append(dummy)

        update(rel_win)
        buffer.append(rel_win)

    # To deal with the special case of the last sample, we process
    # a "dummy" sample just after the last sample.
    if len(buffer) > 0:
        dummy = (buffer[-1][0], buffer[-1][1] + EPSILON)
        update(dummy)

    return dmax


def infer_delta_max_hi(
    release_windows: Iterable[ReleaseWindow], nmax: int | None = None
) -> list[Duration]:
    """Given a sequence of release-time windows, infer a delta-max-hi vector
    that under-approximates the maximal exact-count intervals.

    Returns a vector `v` such that `v[n]` indicates a lower bound on the
    longest interval in which at most `n` releases were observed.

    If `nmax` is provided, a delta-max-lo prefix of length at most `nmax + 1` is
    returned (`nmax` must be positive).
    """

    if nmax is not None and nmax <= 0:
        raise ValueError("nmax must be positive")
    dmax: list[Duration] = []
    buffer: deque[ReleaseWindow] = deque() if nmax is None else deque(maxlen=nmax + 1)

    def update(rel_win: ReleaseWindow):
        # for under-approximation, use lower bound on current release time
        rel_i_lo = rel_win[0]

        for j, rel_win_j in enumerate(buffer):
            # for under-approximation of distance, use upper bound on prior release time
            rel_j_hi = rel_win_j[1]
            count = len(buffer) - j - 1
            if nmax is None or count <= nmax:
                delta = max(0, rel_i_lo - rel_j_hi - EPSILON)
                if len(dmax) == count:
                    dmax.append(delta)
                else:
                    dmax[count] = max(dmax[count], delta)

    for rel_win in release_windows:
        if len(buffer) != 0:
            if buffer[-1][0] > rel_win[0]:
                raise ValueError("release-window lower bounds must be monotonic")
            if buffer[-1][1] > rel_win[1]:
                raise ValueError("release-window upper bounds must be monotonic")
        else:
            # To deal with the special case of the first sample, we insert a
            # "dummy" sample into the buffer just before the first sample.
            dummy = (rel_win[0], rel_win[1] - EPSILON)
            buffer.append(dummy)

        update(rel_win)
        buffer.append(rel_win)

    # To deal with the special case of the last sample, we process
    # a "dummy" sample just after the last sample.
    if len(buffer) > 0:
        dummy = (buffer[-1][0] + EPSILON, buffer[-1][1])
        update(dummy)

    return dmax
