"""Inference of periodic models with offset and jitter from sequences of certain release times."""

from collections.abc import Iterable, Iterator, Sequence
from itertools import chain
from math import ceil, floor
from typing import NamedTuple, TypeAlias

from .iterators import (
    evenly_spaced_around,
    first_and_last_nonoutlier,
    hampel_identifier_nonwindowed,
    obatched,
)
from .time import Duration, Instant


class PeriodicModel(NamedTuple):
    period: Duration
    offset: Instant
    jitter: Duration


Batch: TypeAlias = tuple[tuple[int, Instant], ...]


def validate_periodic_tunables(
    batch_size: int,
    overlap: int,
    n_candidates: int,
    candidate_dispersion: float,
    jitter_pruning_threshold: float,
    jitter_selection_threshold: float,
    negligible_jitter_threshold: int,
) -> None:
    if not (1 <= overlap < batch_size):
        raise ValueError(f"infeasible batch size {batch_size} with overlap {overlap}")
    if n_candidates < 3:
        raise ValueError("n_candidates must be at least 3")
    if candidate_dispersion < 0:
        raise ValueError("candidate_dispersion must be non-negative")
    if jitter_pruning_threshold < 1:
        raise ValueError("jitter_pruning_threshold must be at least 1")
    if jitter_selection_threshold < 1:
        raise ValueError("jitter_selection_threshold must be at least 1")
    if negligible_jitter_threshold < 0:
        raise ValueError("negligible_jitter_threshold must be non-negative")


def trailing_zeroes(n: int) -> int:
    "Return the number of zeroes at the end of the given number in decimal notation."
    count = 0
    while n != 0 and n % 10 == 0:
        count += 1
        n = n // 10
    return count


def truncate_to_max_len(candidates: list[PeriodicModel], max_len: int) -> None:
    # Using something more "fancy" like heapq.nsmallest() or sorted() is not
    # worth it since we're removing at most two elements, and in expectation
    # close to none.
    while len(candidates) > max_len:
        worst_idx = max(range(len(candidates)), key=lambda i: candidates[i].jitter)
        del candidates[worst_idx]


def batch_last_processed_index(
    batch: Batch,
    overlap: int,
) -> int:
    return batch[overlap - 1][0]


def batch_gaps(batch: Batch) -> list[Duration]:
    return list(r2 - r1 for (_, r1), (_, r2) in zip(batch, batch[1:]))


def batch_mean_gap(batch: Batch) -> float:
    if len(batch) > 1:
        total = sum(r2 - r1 for (_, r1), (_, r2) in zip(batch, batch[1:]))
        return total / (len(batch) - 1)
    else:
        return 0


def clean_batch(batch: Batch) -> Batch:
    s, e = first_and_last_nonoutlier(hampel_identifier_nonwindowed(batch_gaps(batch)))
    s = s if s is not None else 0
    e = e + 2 if e is not None else len(batch)
    return tuple(batch[s:e])


def batch_offset(batch: Batch, period: Duration) -> Instant:
    return min((release - i * period for i, release in batch))


def batch_jitter(batch: Batch, offset: Instant, period: Duration) -> Duration:
    return max((release - i * period - offset for i, release in batch))


def batch_model(batch: Batch, period: Duration) -> PeriodicModel:
    offset = batch_offset(batch, period)
    jitter = batch_jitter(batch, offset, period)
    return PeriodicModel(period, offset, jitter)


def batch_min_jitter_model(
    batch: Batch, search_range: tuple[float, float] = (0.5, 2.0)
) -> PeriodicModel:

    mg = batch_mean_gap(batch)
    lo = int(floor(mg * search_range[0]))
    hi = int(ceil(mg * search_range[1]))

    opt = None
    while hi - lo > 1:
        mid = (hi + lo) // 2

        m_lo = batch_model(batch, mid)
        m_hi = batch_model(batch, mid + 1)

        if m_lo.jitter < m_hi.jitter:
            # lower is better => discard upper half of search space
            hi = mid
            opt = m_lo
        else:
            # larger is better => discard lower half of search space
            lo = mid
            opt = m_hi

    return opt if opt is not None else batch_model(batch, int(round(mg)))


def spaced_period_candidates(
    min_jitter_model: PeriodicModel,
    n_candidates: int,
    candidate_dispersion: float,
) -> Iterator[Duration]:
    extension = candidate_dispersion * min_jitter_model.jitter
    return (
        int(round(p))
        for p in evenly_spaced_around(min_jitter_model.period, extension, n_candidates)
        if round(p) > 0
    )


def rounded_at_each_granularity(
    period: Duration, adjustments: Sequence[Duration]
) -> Iterator[Duration]:
    granularity = 10
    while granularity < period:
        rounded = period // granularity
        for delta in adjustments:
            candidate = rounded + delta
            if candidate > 0 and candidate % 10 != 0:
                yield candidate * granularity
        if granularity * 10 >= period:
            # In the last iteration, emit any suppressed candidates last.
            for delta in adjustments:
                candidate = rounded + delta
                if candidate > 0 and candidate % 10 == 0:
                    yield candidate * granularity
        granularity *= 10


def initial_candidate_periods(
    min_jitter_model: PeriodicModel,
    n_candidates: int,
    candidate_dispersion: float,
    rounding_adjustments: Sequence[Duration],
) -> set[Duration]:
    return set(
        chain(
            spaced_period_candidates(
                min_jitter_model, n_candidates, candidate_dispersion
            ),
            rounded_at_each_granularity(min_jitter_model.period, rounding_adjustments),
        )
    )


def derived_model_candidates(
    last_processed_idx: int,
    running_mean_period: Duration,
    min_jitter_period: Duration,
    model_candidates: Sequence[PeriodicModel],
) -> Iterator[PeriodicModel]:
    for ref_period in set((running_mean_period, min_jitter_period)):
        mc_star = min(model_candidates, key=lambda m: abs(m.period - ref_period))
        if mc_star.period < ref_period:
            offset_new = mc_star.offset + last_processed_idx * (
                mc_star.period - ref_period
            )
            jitter_new = mc_star.offset + mc_star.jitter - offset_new
        else:
            offset_new = mc_star.offset
            jitter_new = mc_star.jitter + last_processed_idx * (
                mc_star.period - ref_period
            )
        yield PeriodicModel(period=ref_period, offset=offset_new, jitter=jitter_new)


def batch_update(batch: Batch, mc_old: PeriodicModel) -> PeriodicModel:
    period, jitter, offset = mc_old.period, mc_old.jitter, mc_old.offset
    for i, rel in batch:
        distance = rel - i * period - offset
        if distance < 0:
            offset += distance
            jitter -= distance
        else:
            jitter = max(jitter, distance)
    return PeriodicModel(period=period, offset=offset, jitter=jitter)


def infer_periodic_model(
    releases: Iterable[Instant],
    batch_size: int = 4096,
    overlap: int = 1,
    n_candidates: int = 50,
    candidate_dispersion: float = 3.0,
    rounding_adjustments: Sequence[Duration] = (-2, -1, 0, 1, 2),
    jitter_pruning_threshold: float = 5,
    jitter_selection_threshold: float = 1.25,
    negligible_jitter_threshold: int = 0,
) -> PeriodicModel:
    """The LiME periodic model inference heuristic: given a sequence of releases,
    infer a periodic model that explains all observations while trying
    to minimize the maximum jitter bound."""

    validate_periodic_tunables(
        batch_size,
        overlap,
        n_candidates,
        candidate_dispersion,
        jitter_pruning_threshold,
        jitter_selection_threshold,
        negligible_jitter_threshold,
    )

    model_candidates: list[PeriodicModel] = []
    running_mean: float = 0

    for k, batch in enumerate(
        obatched(enumerate(releases), batch_size, overlap),
        start=1,
    ):
        min_jitter_model = batch_min_jitter_model(clean_batch(batch))
        if k == 1:
            # first batch
            running_mean = min_jitter_model.period
            # generate model candidates
            model_candidates = [
                batch_model(batch, p)
                for p in initial_candidate_periods(
                    min_jitter_model,
                    n_candidates,
                    candidate_dispersion,
                    rounding_adjustments,
                )
            ]

        else:
            # subsequent batch
            running_mean = running_mean + (min_jitter_model.period - running_mean) / k

            # Take stock of how many candidates we have.
            mc_count = len(model_candidates)

            # Derive some new model candidates.
            dmc = derived_model_candidates(
                batch_last_processed_index(batch, overlap),
                int(round(running_mean)),
                min_jitter_model.period,
                model_candidates,
            )

            # Update all model candidates.
            updated_candidates = set(
                batch_update(batch, mc) for mc in chain(model_candidates, dmc)
            )

            # Finally, prune any candidates that have diverged too much from
            # the best-so-far solution.
            best = min(updated_candidates, key=lambda m: m.jitter)
            model_candidates = [
                mc
                for mc in updated_candidates
                if mc.jitter <= negligible_jitter_threshold
                or mc.jitter <= best.jitter * jitter_pruning_threshold
            ]

            # Ensure the candidate set doesn't grow.
            truncate_to_max_len(model_candidates, mc_count)

    # All batches processed, now choose the best remaining model.

    # First, identify the model that overall minimizes the maximum jitter.
    best = min(model_candidates, key=lambda m: m.jitter)
    # Second, retain only models that are within the acceptable jitter threshold.
    acceptable = (
        m
        for m in model_candidates
        if m.jitter <= negligible_jitter_threshold
        or m.jitter <= best.jitter * jitter_selection_threshold
    )
    # Third, return the largest-granularity one (= most trailing zeroes) with the least jitter.
    return max(acceptable, key=lambda m: (trailing_zeroes(m.period), -m.jitter))
