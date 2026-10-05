"""Inference of periodic models with offset and jitter from sequences of uncertain release-time windows."""

from collections.abc import Iterable, Sequence
from itertools import chain
from math import ceil, floor
from typing import TypeAlias

from rt_model_inference.certain_periodic import (
    PeriodicModel,
    derived_model_candidates,
    initial_candidate_periods,
    trailing_zeroes,
    truncate_to_max_len,
    validate_periodic_tunables,
)

from .iterators import (
    first_and_last_nonoutlier,
    hampel_identifier_nonwindowed,
    obatched,
)
from .time import Duration, Instant, ReleaseWindow

Batch: TypeAlias = tuple[tuple[int, ReleaseWindow], ...]

# symbolic constants for release-window tuples
LOWER = 0
UPPER = 1


def batch_last_processed_index(
    batch: Batch,
    overlap: int,
) -> int:
    return batch[overlap - 1][0]


def batch_gaps(batch: Batch) -> list[Duration]:
    # consider the separation between release-window upper bounds
    return list(w2[UPPER] - w1[UPPER] for (_, w1), (_, w2) in zip(batch, batch[1:]))


def batch_mean_gap(batch: Batch) -> float:
    if len(batch) > 1:
        total = sum(w2[UPPER] - w1[UPPER] for (_, w1), (_, w2) in zip(batch, batch[1:]))
        return total / (len(batch) - 1)
    else:
        return 0


def clean_batch(batch: Batch) -> Batch:
    s, e = first_and_last_nonoutlier(hampel_identifier_nonwindowed(batch_gaps(batch)))
    s = s if s is not None else 0
    e = e + 2 if e is not None else len(batch)
    return tuple(batch[s:e])


# ############################### CERTAIN FIT INFERENCE #################################


def certain_fit_batch_offset(batch: Batch, period: Duration) -> Instant:
    # certain fit: use lower bound on release time for offset estimation
    return min((window[LOWER] - i * period for i, window in batch))


def certain_fit_batch_jitter(
    batch: Batch, offset: Instant, period: Duration
) -> Duration:
    # certain fit: use upper bound on release time for jitter estimation
    return max((window[UPPER] - i * period - offset for i, window in batch))


def certain_fit_batch_model(batch: Batch, period: Duration) -> PeriodicModel:
    offset = certain_fit_batch_offset(batch, period)
    jitter = certain_fit_batch_jitter(batch, offset, period)
    return PeriodicModel(period, offset, jitter)


def certain_fit_batch_min_jitter_model(
    batch: Batch, search_range: tuple[float, float] = (0.5, 2.0)
) -> PeriodicModel:

    mg = batch_mean_gap(batch)
    lo = int(floor(mg * search_range[0]))
    hi = int(ceil(mg * search_range[1]))

    opt = None
    while hi - lo > 1:
        mid = (hi + lo) // 2

        m_lo = certain_fit_batch_model(batch, mid)
        m_hi = certain_fit_batch_model(batch, mid + 1)

        if m_lo.jitter < m_hi.jitter:
            # lower is better => discard upper half of search space
            hi = mid
            opt = m_lo
        else:
            # larger is better => discard lower half of search space
            lo = mid
            opt = m_hi

    return opt if opt is not None else certain_fit_batch_model(batch, int(round(mg)))


def certain_fit_batch_update(batch: Batch, mc_old: PeriodicModel) -> PeriodicModel:
    period, jitter, offset = mc_old.period, mc_old.jitter, mc_old.offset
    for i, window in batch:
        # To ensure the entire interval is covered, use the lower bound when
        # considering the offset.
        distance = window[LOWER] - i * period - offset
        if distance < 0:
            offset += distance
            jitter -= distance
        # When considering the maximum release jitter, use the upper bound on the release time.
        distance = window[UPPER] - i * period - offset
        jitter = max(jitter, distance)
    return PeriodicModel(period=period, offset=offset, jitter=jitter)


def infer_certain_fit_periodic_model(
    release_windows: Iterable[ReleaseWindow],
    batch_size: int = 4096,
    overlap: int = 1,
    n_candidates: int = 50,
    candidate_dispersion: float = 3.0,
    rounding_adjustments: Sequence[Duration] = (-2, -1, 0, 1, 2),
    jitter_pruning_threshold: float = 5,
    jitter_selection_threshold: float = 1.25,
    negligible_jitter_threshold: int = 0,
) -> PeriodicModel:
    """The LiME certain-fit periodic model inference heuristic: given
    a sequence of release windows, infer a periodic model
    that fully covers all release windows while trying to minimize the
    maximum jitter bound."""

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
        obatched(enumerate(release_windows), batch_size, overlap),
        start=1,
    ):
        min_jitter_model = certain_fit_batch_min_jitter_model(clean_batch(batch))
        if k == 1:
            # first batch
            running_mean = min_jitter_model.period
            # generate model candidates
            model_candidates = [
                certain_fit_batch_model(batch, p)
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
                certain_fit_batch_update(batch, mc)
                for mc in chain(model_candidates, dmc)
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


# ############################### POSSIBLE FIT INFERENCE ################################


def possible_fit_batch_offset(batch: Batch, period: Duration) -> Instant:
    # possible fit: use upper bound on release time for offset estimation
    return min((window[UPPER] - i * period for i, window in batch))


def possible_fit_batch_jitter(
    batch: Batch, offset: Instant, period: Duration
) -> Duration:
    # possible fit: use lower bound on release time for jitter estimation
    # NB: This can result in NEGATIVE jitter. This is intentional and
    #     corrected for before the final model is returned to the caller.
    return max((window[LOWER] - i * period - offset for i, window in batch))


def possible_fit_batch_model(batch: Batch, period: Duration) -> PeriodicModel:
    offset = possible_fit_batch_offset(batch, period)
    jitter = possible_fit_batch_jitter(batch, offset, period)
    return PeriodicModel(period, offset, jitter)


def possible_fit_batch_min_jitter_model(
    batch: Batch, search_range: tuple[float, float] = (0.5, 2.0)
) -> PeriodicModel:

    mg = batch_mean_gap(batch)
    lo = int(floor(mg * search_range[0]))
    hi = int(ceil(mg * search_range[1]))

    opt = None
    while hi - lo > 1:
        mid = (hi + lo) // 2

        m_lo = possible_fit_batch_model(batch, mid)
        m_hi = possible_fit_batch_model(batch, mid + 1)

        if m_lo.jitter < m_hi.jitter:
            # lower is better => discard upper half of search space
            hi = mid
            opt = m_lo
        else:
            # larger is better => discard lower half of search space
            lo = mid
            opt = m_hi

    return opt if opt is not None else possible_fit_batch_model(batch, int(round(mg)))


def possible_fit_batch_update(batch: Batch, mc_old: PeriodicModel) -> PeriodicModel:
    period, jitter, offset = mc_old.period, mc_old.jitter, mc_old.offset
    for i, window in batch:
        # To ensure the interval is intersected, use the upper bound when
        # considering the offset.
        distance = window[UPPER] - i * period - offset
        if distance < 0:
            offset += distance
            jitter -= distance
        # When considering the maximum release jitter, use the lower bound on the release time.
        distance = window[LOWER] - i * period - offset
        jitter = max(jitter, distance)
    return PeriodicModel(period=period, offset=offset, jitter=jitter)


def infer_possible_fit_periodic_model(
    release_windows: Iterable[ReleaseWindow],
    batch_size: int = 4096,
    overlap: int = 1,
    n_candidates: int = 50,
    candidate_dispersion: float = 3.0,
    rounding_adjustments: Sequence[Duration] = (-2, -1, 0, 1, 2),
    jitter_pruning_threshold: float = 5,
    jitter_selection_threshold: float = 1.25,
    negligible_jitter_threshold: int = 0,
) -> PeriodicModel:
    """The LiME possible-fit periodic model inference heuristic: given
    a sequence of release windows, infer a periodic model
    that intersects all release windows while trying to minimize the
    maximum jitter bound."""

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
        obatched(enumerate(release_windows), batch_size, overlap),
        start=1,
    ):
        min_jitter_model = possible_fit_batch_min_jitter_model(clean_batch(batch))
        if k == 1:
            # first batch
            running_mean = min_jitter_model.period
            # generate model candidates
            model_candidates = [
                possible_fit_batch_model(batch, p)
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
                possible_fit_batch_update(batch, mc)
                for mc in chain(model_candidates, dmc)
            )

            # Finally, prune any candidates that have diverged too much from
            # the best-so-far solution with a non-trivial jitter bound.
            best = min(
                (m for m in updated_candidates if m.jitter > 0),
                key=lambda m: m.jitter,
                default=None,
            )
            if best is not None:
                model_candidates = [
                    mc
                    for mc in updated_candidates
                    if mc.jitter <= negligible_jitter_threshold
                    or mc.jitter <= best.jitter * jitter_pruning_threshold
                ]
            else:
                model_candidates = list(updated_candidates)

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
    selected = max(acceptable, key=lambda m: (trailing_zeroes(m.period), -m.jitter))
    return PeriodicModel(
        period=selected.period, offset=selected.offset, jitter=max(0, selected.jitter)
    )
