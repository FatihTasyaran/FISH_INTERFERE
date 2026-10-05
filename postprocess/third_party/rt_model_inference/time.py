from typing import TypeAlias

# Discrete time: the smallest time unit is 1.
EPSILON = 1

# Various type aliases of `int` to express different kinds of time-related concepts.
Instant: TypeAlias = int
Duration: TypeAlias = int

ReleaseWindow: TypeAlias = tuple[Instant, Instant]
