from collections.abc import Iterable, Iterator, Sequence
from itertools import accumulate
from statistics import median
from typing import TypeVar

from rt_model_inference.time import Duration

T = TypeVar("T")


def obatched(
    it: Iterable[T], batch_size: int, overlap: int = 1
) -> Iterable[tuple[T, ...]]:
    """Similarly to `batched` from the `itertools` module,
    expose a given iterator as a sequence of batches, but do so with a configurable
    amount of overlap among consecutive batches."""

    if not (0 <= overlap < batch_size):
        raise ValueError(f"infeasible batch size {batch_size} with overlap {overlap}")

    batch: list[T] = []
    only_overlap = True
    for x in it:
        batch.append(x)
        only_overlap = False
        if len(batch) == batch_size:
            yield tuple(batch)
            batch = batch[-overlap:] if overlap > 0 else []
            only_overlap = True

    if not only_overlap:
        yield tuple(batch)


def hampel_identifier_nonwindowed(
    data: Sequence[float | int],
    n_sigmas: float = 3.0,
    scale: float = 1,
) -> Iterator[bool]:
    """
    Pure-Python, non-windowed Hampel-like identifier for lists.
    Made for simplicity, not speed.

    - Yields exactly one boolean per input element.
    - Does not use a sliding window, but considers the median of
      the whole sequence at once.
    """

    m = median(data)
    mad = median(abs(v - m) for v in data)

    for v in data:
        if mad == 0:
            yield False
        else:
            robust_sigma = scale * mad
            yield abs(v - m) > (n_sigmas * robust_sigma)


def first_and_last_outlier(
    outlier_flags: Iterable[bool],
) -> tuple[int | None, int | None]:
    """Return the index of the first and the last outlier samples (if any),
    as determined by the given sequence of outlier flags."""

    first = None
    last = None

    for i, flagged in enumerate(outlier_flags):
        if flagged:
            if first is None:
                first = i
            last = i

    return (first, last)


def first_and_last_nonoutlier(
    outlier_flags: Iterable[bool],
) -> tuple[int | None, int | None]:
    """Return the index of the first and the last non-outlier samples (if any),
    as determined by the given sequence of outlier flags."""

    first = None
    last = None

    for i, flagged in enumerate(outlier_flags):
        if not flagged:
            if first is None:
                first = i
            last = i

    return (first, last)


def is_monotonic(releases: Sequence[Duration]) -> bool:
    "Check whether the given sequence is monotonic."
    return all(x <= y for x, y in zip(releases, releases[1:]))


def monotonic(values: Iterable[int]) -> Iterator[int]:
    "Force the given sequence to be monotonic."
    return accumulate(values, max)


def evenly_spaced_around(center: float, extension: float, n: int) -> Iterator[float]:
    # Always include the center.
    yield center
    # Then yield n-1 steps around the center, in increments of 2/(n - 2) * extension.
    lo = center - extension
    spread = 2 * extension
    for k in range(n - 1):
        yield lo + spread * float(k) / (n - 2)


def is_empty(it: Iterable[T]) -> bool:
    try:
        _ = next(iter(it))
        return False
    except StopIteration:
        return True
