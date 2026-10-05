from collections.abc import Iterable, Sequence
from itertools import chain

from rt_model_inference.certain_periodic import (
    PeriodicModel,
    derived_model_candidates,
    initial_candidate_periods,
    trailing_zeroes,
    truncate_to_max_len,
    validate_periodic_tunables,
)
from rt_model_inference.time import Duration, ReleaseWindow
from rt_model_inference.uncertain_periodic import (
    Batch,
    batch_last_processed_index,
    certain_fit_batch_min_jitter_model,
    certain_fit_batch_model,
    certain_fit_batch_update,
    clean_batch,
    possible_fit_batch_min_jitter_model,
    possible_fit_batch_model,
    possible_fit_batch_update,
)


class CertainFitPeriodicExtractor:
    """Streaming extractor for the certain-fit periodic task model
    with release jitter and offset."""

    def __init__(
        self,
        batch_size: int = 4096,
        overlap: int = 1,
        n_candidates: int = 50,
        candidate_dispersion: float = 3.0,
        rounding_adjustments: Sequence[Duration] = (-2, -1, 0, 1, 2),
        jitter_pruning_threshold: float = 5,
        jitter_selection_threshold: float = 1.25,
        negligible_jitter_threshold: int = 0,
    ):
        validate_periodic_tunables(
            batch_size,
            overlap,
            n_candidates,
            candidate_dispersion,
            jitter_pruning_threshold,
            jitter_selection_threshold,
            negligible_jitter_threshold,
        )

        self._batch_size: int = batch_size
        self._overlap: int = overlap
        self._n_candidates: int = n_candidates
        self._candidate_dispersion: float = candidate_dispersion
        self._rounding_adjustments: Sequence[Duration] = rounding_adjustments
        self._jitter_pruning_threshold: float = jitter_pruning_threshold
        self._jitter_selection_threshold: float = jitter_selection_threshold
        self._negligible_jitter_threshold: int = negligible_jitter_threshold

        self._next_idx: int = 0
        self._batch_count: int = 1
        self._current_batch: list[tuple[int, ReleaseWindow]] = []
        self._candidates: list[PeriodicModel] = []
        self._running_mean: float = 0

    def _process_first_batch(self, batch: Batch) -> tuple[float, list[PeriodicModel]]:
        min_jitter_model = certain_fit_batch_min_jitter_model(clean_batch(batch))
        running_mean = float(min_jitter_model.period)
        candidates = [
            certain_fit_batch_model(batch, p)
            for p in initial_candidate_periods(
                min_jitter_model,
                self._n_candidates,
                self._candidate_dispersion,
                self._rounding_adjustments,
            )
        ]
        return running_mean, candidates

    def _process_subsequent_batch(
        self, batch: Batch
    ) -> tuple[float, list[PeriodicModel]]:
        min_jitter_model = certain_fit_batch_min_jitter_model(clean_batch(batch))

        running_mean = (
            self._running_mean
            + (min_jitter_model.period - self._running_mean) / self._batch_count
        )

        # update candidates
        dmc = derived_model_candidates(
            batch_last_processed_index(batch, self._overlap),
            int(round(running_mean)),
            min_jitter_model.period,
            self._candidates,
        )
        candidates = set(
            certain_fit_batch_update(batch, mc) for mc in chain(self._candidates, dmc)
        )

        # prune candidates
        best = min(candidates, key=lambda m: m.jitter)
        candidates = [
            mc
            for mc in candidates
            if mc.jitter <= self._negligible_jitter_threshold
            or mc.jitter <= best.jitter * self._jitter_pruning_threshold
        ]

        # Ensure the candidate set doesn't grow.
        truncate_to_max_len(candidates, len(self._candidates))

        return running_mean, candidates

    def _is_first_batch(self) -> bool:
        return not self._candidates

    def _consume_batch(self):
        self._running_mean, self._candidates = (
            self._process_first_batch(tuple(self._current_batch))
            if self._is_first_batch()
            else self._process_subsequent_batch(tuple(self._current_batch))
        )
        self._current_batch = (
            self._current_batch[-self._overlap :] if self._overlap > 0 else []
        )
        self._batch_count += 1

    def _batch_contains_only_overlap(self) -> bool:
        return not self._is_first_batch() and len(self._current_batch) == self._overlap

    def feed(self, release_windows: Iterable[ReleaseWindow]) -> None:
        "Update the current model estimate based on newly observed release windows."
        i = None
        for i, r in enumerate(release_windows, start=self._next_idx):
            self._current_batch.append((i, r))
            if len(self._current_batch) == self._batch_size:
                self._consume_batch()
        if i is not None:
            self._next_idx = i + 1

    def __call__(self, release_windows: Iterable[ReleaseWindow]) -> None:
        self.feed(release_windows)

    @property
    def current_model(self) -> PeriodicModel | None:
        "Query the model extracted so far."

        # First, consume any remaining releases in the current batch.
        if self._is_first_batch():
            if len(self._current_batch) < 2:
                # not enough seen yet
                return None
            _, candidates = self._process_first_batch(tuple(self._current_batch))
        elif not self._batch_contains_only_overlap():
            _, candidates = self._process_subsequent_batch(tuple(self._current_batch))
        else:
            # nothing to process, work with the current estimates
            candidates = self._candidates

        # First, identify the model that overall minimizes the maximum jitter.
        best = min(candidates, key=lambda m: m.jitter)
        # Second, retain only models that are within the acceptable jitter threshold.
        acceptable = (
            m
            for m in candidates
            if m.jitter <= self._negligible_jitter_threshold
            or m.jitter <= best.jitter * self._jitter_selection_threshold
        )
        # Third, return the largest-granularity one (= most trailing zeroes) with the least jitter.
        selected = max(acceptable, key=lambda m: (trailing_zeroes(m.period), -m.jitter))
        return PeriodicModel(
            period=selected.period,
            offset=selected.offset,
            jitter=selected.jitter,
        )


class PossibleFitPeriodicExtractor:
    """Streaming extractor for the possible-fit periodic task model
    with release jitter and offset."""

    def __init__(
        self,
        batch_size: int = 4096,
        overlap: int = 1,
        n_candidates: int = 50,
        candidate_dispersion: float = 3.0,
        rounding_adjustments: Sequence[Duration] = (-2, -1, 0, 1, 2),
        jitter_pruning_threshold: float = 5,
        jitter_selection_threshold: float = 1.25,
        negligible_jitter_threshold: int = 0,
    ):
        validate_periodic_tunables(
            batch_size,
            overlap,
            n_candidates,
            candidate_dispersion,
            jitter_pruning_threshold,
            jitter_selection_threshold,
            negligible_jitter_threshold,
        )

        self._batch_size: int = batch_size
        self._overlap: int = overlap
        self._n_candidates: int = n_candidates
        self._candidate_dispersion: float = candidate_dispersion
        self._rounding_adjustments: Sequence[Duration] = rounding_adjustments
        self._jitter_pruning_threshold: float = jitter_pruning_threshold
        self._jitter_selection_threshold: float = jitter_selection_threshold
        self._negligible_jitter_threshold: int = negligible_jitter_threshold

        self._next_idx: int = 0
        self._batch_count: int = 1
        self._current_batch: list[tuple[int, ReleaseWindow]] = []
        self._candidates: list[PeriodicModel] = []
        self._running_mean: float = 0

    def _process_first_batch(self, batch: Batch) -> tuple[float, list[PeriodicModel]]:
        min_jitter_model = possible_fit_batch_min_jitter_model(clean_batch(batch))
        running_mean = float(min_jitter_model.period)
        candidates = [
            possible_fit_batch_model(batch, p)
            for p in initial_candidate_periods(
                min_jitter_model,
                self._n_candidates,
                self._candidate_dispersion,
                self._rounding_adjustments,
            )
        ]
        return running_mean, candidates

    def _process_subsequent_batch(
        self, batch: Batch
    ) -> tuple[float, list[PeriodicModel]]:
        min_jitter_model = possible_fit_batch_min_jitter_model(clean_batch(batch))

        running_mean = (
            self._running_mean
            + (min_jitter_model.period - self._running_mean) / self._batch_count
        )

        # update candidates
        dmc = derived_model_candidates(
            batch_last_processed_index(batch, self._overlap),
            int(round(running_mean)),
            min_jitter_model.period,
            self._candidates,
        )
        candidates = set(
            possible_fit_batch_update(batch, mc) for mc in chain(self._candidates, dmc)
        )

        # prune candidates
        best = min(
            (m for m in candidates if m.jitter > 0),
            key=lambda m: m.jitter,
            default=None,
        )
        if best is not None:
            candidates = [
                mc
                for mc in candidates
                if mc.jitter <= self._negligible_jitter_threshold
                or mc.jitter <= best.jitter * self._jitter_pruning_threshold
            ]
        else:
            candidates = list(candidates)

        # Ensure the candidate set doesn't grow.
        truncate_to_max_len(candidates, len(self._candidates))

        return running_mean, candidates

    def _is_first_batch(self) -> bool:
        return not self._candidates

    def _consume_batch(self):
        self._running_mean, self._candidates = (
            self._process_first_batch(tuple(self._current_batch))
            if self._is_first_batch()
            else self._process_subsequent_batch(tuple(self._current_batch))
        )
        self._current_batch = (
            self._current_batch[-self._overlap :] if self._overlap > 0 else []
        )
        self._batch_count += 1

    def _batch_contains_only_overlap(self) -> bool:
        return not self._is_first_batch() and len(self._current_batch) == self._overlap

    def feed(self, release_windows: Iterable[ReleaseWindow]) -> None:
        "Update the current model estimate based on newly observed release windows."
        i = None
        for i, r in enumerate(release_windows, start=self._next_idx):
            self._current_batch.append((i, r))
            if len(self._current_batch) == self._batch_size:
                self._consume_batch()
        if i is not None:
            self._next_idx = i + 1

    def __call__(self, release_windows: Iterable[ReleaseWindow]) -> None:
        self.feed(release_windows)

    @property
    def current_model(self) -> PeriodicModel | None:
        "Query the model extracted so far."

        # First, consume any remaining releases in the current batch.
        if self._is_first_batch():
            if len(self._current_batch) < 2:
                # not enough seen yet
                return None
            _, candidates = self._process_first_batch(tuple(self._current_batch))
        elif not self._batch_contains_only_overlap():
            _, candidates = self._process_subsequent_batch(tuple(self._current_batch))
        else:
            # nothing to process, work with the current estimates
            candidates = self._candidates

        # First, identify the model that overall minimizes the maximum jitter.
        best = min(candidates, key=lambda m: m.jitter)
        # Second, retain only models that are within the acceptable jitter threshold.
        acceptable = (
            m
            for m in candidates
            if m.jitter <= self._negligible_jitter_threshold
            or m.jitter <= best.jitter * self._jitter_selection_threshold
        )
        # Third, return the largest-granularity one (= most trailing zeroes) with the least jitter.
        selected = max(acceptable, key=lambda m: (trailing_zeroes(m.period), -m.jitter))
        return PeriodicModel(
            period=selected.period,
            offset=selected.offset,
            jitter=max(0, selected.jitter),
        )
