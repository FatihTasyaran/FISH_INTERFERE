"""Continuously updating streaming model extractors for sporadic task models."""

from collections import deque
from collections.abc import Iterable

from rt_model_inference.time import EPSILON, Duration, Instant


class SporadicExtractor:
    """Trivial streaming extractor for the classic sporadic
    task model defined by a scalar minimum inter-arrival time.
    """

    def __init__(self):
        self._last_release: Instant | None = None
        self._min_separation: Duration | None = None

    def feed(self, releases: Iterable[Instant]) -> None:
        "Update the current model estimate based on newly observed releases."
        for r in releases:
            if self._last_release is not None:
                separation = r - self._last_release
                if separation < 0:
                    raise ValueError("releases must be monotonic")
                if self._min_separation is None:
                    self._min_separation = separation
                else:
                    self._min_separation = min(self._min_separation, separation)
            self._last_release = r

    def __call__(self, releases: Iterable[Instant]) -> None:
        self.feed(releases)

    @property
    def current_model(self) -> Duration | None:
        "Query the model extracted so far."
        return self._min_separation


class DeltaMinExtractor:
    "Streaming extractor for delta-min vectors."

    def __init__(self, nmax: int | None = None):
        if nmax is not None and nmax <= 1:
            raise ValueError("nmax must be at least 2")
        self._nmax: int | None = nmax

        self._dmin: list[Duration] = [0, 1]
        self._last_i: int = -1
        self._buffer: deque[Instant] = (
            deque() if nmax is None else deque(maxlen=nmax - 1)
        )

    def feed(self, releases: Iterable[Instant]) -> None:
        "Update the current model estimate based on newly observed releases."
        buffer = self._buffer
        dmin = self._dmin
        i = self._last_i
        for i, rel_i in enumerate(releases, start=self._last_i + 1):
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
        self._last_i = i

    def __call__(self, releases: Iterable[Instant]) -> None:
        self.feed(releases)

    @property
    def current_model(self) -> list[Duration]:
        "Query the model extracted so far."
        if len(self._buffer) == 0:
            # we didn't see any releases
            return []
        else:
            return list(self._dmin)


class DeltaMaxExtractor:
    "Streaming extractor for delta-max vectors."

    def __init__(self, nmax: int | None = None):
        if nmax is not None and nmax <= 0:
            raise ValueError("nmax must be positive")
        self._nmax: int | None = nmax

        self._dmax: list[Duration] = []
        self._buffer: deque[Instant] = (
            deque() if nmax is None else deque(maxlen=nmax + 1)
        )

    def _update_dmax(self, dmax: list[Duration], rel_i: Instant):
        for j, rel_j in enumerate(self._buffer):
            # Account for the open interval defined by rel_j and rel_i, including
            # neither end point.
            count = len(self._buffer) - j - 1
            if self._nmax is None or count <= self._nmax:
                delta = max(0, rel_i - rel_j - EPSILON)
                if len(dmax) == count:
                    dmax.append(delta)
                else:
                    dmax[count] = max(dmax[count], delta)

    def feed(self, releases: Iterable[Instant]) -> None:
        "Update the current model estimate based on newly observed releases."
        for rel_i in releases:
            if len(self._buffer) != 0:
                if self._buffer[-1] > rel_i:
                    raise ValueError("releases must be monotonic")
            else:
                # ensure the first observation is fully reflected by inserting
                # a dummy sample just before it
                self._buffer.append(rel_i - EPSILON)

            self._update_dmax(self._dmax, rel_i)
            self._buffer.append(rel_i)

    def __call__(self, releases: Iterable[Instant]) -> None:
        self.feed(releases)

    @property
    def current_model(self) -> list[Duration]:
        "Query the model extracted so far."

        dmax = list(self._dmax)

        if self._buffer:
            # ensure the last observation so far is fully reflected by processing
            # a dummy sample after it
            self._update_dmax(dmax, self._buffer[-1] + EPSILON)

        return dmax
