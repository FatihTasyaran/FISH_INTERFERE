"""Continuously updating streaming model extractors for approximated sporadic task models."""

from collections import deque
from collections.abc import Iterable

from rt_model_inference.time import EPSILON, Duration, ReleaseWindow


class DeltaMinHiExtractor:
    "Streaming extractor for delta-min-hi vectors."

    def __init__(self, nmax: int | None = None):
        if nmax is not None and nmax <= 1:
            raise ValueError("nmax must be at least 2")
        self._nmax: int | None = nmax

        self._dmin: list[Duration] = [0, 1]
        self._last_i: int = -1
        self._buffer: deque[ReleaseWindow] = (
            deque() if nmax is None else deque(maxlen=nmax - 1)
        )

    def feed(self, release_windows: Iterable[ReleaseWindow]) -> None:
        "Update the current model estimate based on newly observed release windows."
        buffer = self._buffer
        dmin = self._dmin
        i = self._last_i
        for i, rel_win in enumerate(release_windows, start=self._last_i + 1):
            rel_i_lo = rel_win[0]
            if len(buffer) != 0:
                if buffer[-1][0] > rel_win[0]:
                    raise ValueError("release-window lower bounds must be monotonic")
                if buffer[-1][1] > rel_win[1]:
                    raise ValueError("release-window upper bounds must be monotonic")
            for j, rel_win_j in enumerate(buffer, start=(i - len(buffer))):
                rel_j_hi = rel_win_j[1]  # use the upper bound on the prior release time
                count = i - j + 1
                delta = max(EPSILON, rel_i_lo - rel_j_hi + EPSILON)
                if len(dmin) == count:
                    dmin.append(delta)
                else:
                    dmin[count] = min(dmin[count], delta)
            buffer.append(rel_win)
        self._last_i = i

    def __call__(self, releases: Iterable[ReleaseWindow]) -> None:
        self.feed(releases)

    @property
    def current_model(self) -> list[Duration]:
        "Query the model extracted so far."
        if len(self._buffer) == 0:
            # we didn't see any releases
            return []
        else:
            return list(self._dmin)


class DeltaMinLoExtractor:
    "Streaming extractor for delta-min-lo vectors."

    def __init__(self, nmax: int | None = None):
        if nmax is not None and nmax <= 1:
            raise ValueError("nmax must be at least 2")
        self._nmax: int | None = nmax

        self._dmin: list[Duration] = [0, 1]
        self._last_i: int = -1
        self._buffer: deque[ReleaseWindow] = (
            deque() if nmax is None else deque(maxlen=nmax - 1)
        )

    def feed(self, release_windows: Iterable[ReleaseWindow]) -> None:
        "Update the current model estimate based on newly observed release windows."
        buffer = self._buffer
        dmin = self._dmin
        i = self._last_i
        for i, rel_win in enumerate(release_windows, start=self._last_i + 1):
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
        self._last_i = i

    def __call__(self, releases: Iterable[ReleaseWindow]) -> None:
        self.feed(releases)

    @property
    def current_model(self) -> list[Duration]:
        "Query the model extracted so far."
        if len(self._buffer) == 0:
            # we didn't see any releases
            return []
        else:
            return list(self._dmin)


class DeltaMaxHiExtractor:
    "Streaming extractor for delta-max-hi vectors."

    def __init__(self, nmax: int | None = None):
        if nmax is not None and nmax <= 0:
            raise ValueError("nmax must be positive")
        self._nmax: int | None = nmax

        self._dmax: list[Duration] = []
        self._buffer: deque[ReleaseWindow] = (
            deque() if nmax is None else deque(maxlen=nmax + 1)
        )

    def _update_dmax(self, dmax: list[Duration], rel_win: ReleaseWindow):
        # for under-approximation, use lower bound on current release time
        rel_i_lo = rel_win[0]
        for j, rel_win_j in enumerate(self._buffer):
            # for under-approximation of distance, use upper bound on prior release time
            rel_j_hi = rel_win_j[1]
            # Account for the open interval defined by rel_j and rel_i, including
            # neither end point.
            count = len(self._buffer) - j - 1
            if self._nmax is None or count <= self._nmax:
                delta = max(0, rel_i_lo - rel_j_hi - EPSILON)
                if len(dmax) == count:
                    dmax.append(delta)
                else:
                    dmax[count] = max(dmax[count], delta)

    def feed(self, release_windows: Iterable[ReleaseWindow]) -> None:
        "Update the current model estimate based on newly observed release windows."
        for rel_win in release_windows:
            if len(self._buffer) != 0:
                if self._buffer[-1][0] > rel_win[0]:
                    raise ValueError("release-window lower bounds must be monotonic")
                if self._buffer[-1][1] > rel_win[1]:
                    raise ValueError("release-window upper bounds must be monotonic")
            else:
                # To deal with the special case of the first sample, we insert a
                # "dummy" sample into the buffer just before the first sample.
                dummy = (rel_win[0], rel_win[1] - EPSILON)
                self._buffer.append(dummy)

            self._update_dmax(self._dmax, rel_win)
            self._buffer.append(rel_win)

    def __call__(self, release_windows: Iterable[ReleaseWindow]) -> None:
        self.feed(release_windows)

    @property
    def current_model(self) -> list[Duration]:
        "Query the model extracted so far."

        dmax = list(self._dmax)

        if self._buffer:
            # ensure the last observation so far is fully reflected by processing
            # a dummy sample after it
            dummy = (self._buffer[-1][0] + EPSILON, self._buffer[-1][1])
            self._update_dmax(dmax, dummy)

        return dmax


class DeltaMaxLoExtractor:
    "Streaming extractor for delta-max-lo vectors."

    def __init__(self, nmax: int | None = None):
        if nmax is not None and nmax <= 0:
            raise ValueError("nmax must be positive")
        self._nmax: int | None = nmax

        self._dmax: list[Duration] = []
        self._buffer: deque[ReleaseWindow] = (
            deque() if nmax is None else deque(maxlen=nmax + 1)
        )

    def _update_dmax(self, dmax: list[Duration], rel_win: ReleaseWindow):
        # for over-approximation, use upper bound on current release time
        rel_i_hi = rel_win[1]
        for j, rel_win_j in enumerate(self._buffer):
            rel_j_lo = rel_win_j[0]
            # Account for the open interval defined by rel_j and rel_i, including
            # neither end point.
            count = len(self._buffer) - j - 1
            if self._nmax is None or count <= self._nmax:
                delta = max(0, rel_i_hi - rel_j_lo - EPSILON)
                if len(dmax) == count:
                    dmax.append(delta)
                else:
                    dmax[count] = max(dmax[count], delta)

    def feed(self, release_windows: Iterable[ReleaseWindow]) -> None:
        "Update the current model estimate based on newly observed release windows."
        for rel_win in release_windows:
            if len(self._buffer) != 0:
                if len(self._buffer) != 0:
                    if self._buffer[-1][0] > rel_win[0]:
                        raise ValueError(
                            "release-window lower bounds must be monotonic"
                        )
                    if self._buffer[-1][1] > rel_win[1]:
                        raise ValueError(
                            "release-window upper bounds must be monotonic"
                        )
            else:
                # To deal with the special case of the first sample, we insert a
                # "dummy" sample into the buffer just before the first sample.
                dummy = (rel_win[0] - EPSILON, rel_win[1])
                self._buffer.append(dummy)

            self._update_dmax(self._dmax, rel_win)
            self._buffer.append(rel_win)

    def __call__(self, release_windows: Iterable[ReleaseWindow]) -> None:
        self.feed(release_windows)

    @property
    def current_model(self) -> list[Duration]:
        "Query the model extracted so far."

        dmax = list(self._dmax)

        if self._buffer:
            # ensure the last observation so far is fully reflected by processing
            # a dummy sample after it
            dummy = (self._buffer[-1][0], self._buffer[-1][1] + EPSILON)
            self._update_dmax(dmax, dummy)

        return dmax
