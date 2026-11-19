"""Select k of n points in [0,1]^3 with low L-infinity star discrepancy.

Start from the candidates nearest a scrambled Sobol set, then swap single points
while the exact discrepancy falls, using part of the time budget.
"""

import time

import numpy as np
from scipy.stats import qmc


def discrepancy(pts):
    """Exact star discrepancy of a small point set in [0,1]^3."""
    k = len(pts)
    grids = [np.unique(np.append(pts[:, d], 1.0)) for d in range(3)]
    idx = [np.searchsorted(grids[d], pts[:, d]) for d in range(3)]
    hist = np.zeros(tuple(len(g) for g in grids), dtype=np.int64)
    np.add.at(hist, tuple(idx), 1)
    closed = hist.cumsum(0).cumsum(1).cumsum(2)
    opened = np.zeros_like(closed)
    opened[1:, 1:, 1:] = closed[:-1, :-1, :-1]
    vol = grids[0][:, None, None] * grids[1][None, :, None] * grids[2][None, None, :]
    return float(max((closed / k - vol).max(), (vol - opened / k).max()))


def select(points, k, seed, time_budget_s):
    start = time.monotonic()
    deadline = start + 0.5 * time_budget_s
    rng = np.random.default_rng(seed)
    points = np.asarray(points, dtype=np.float64)
    n = len(points)
    targets = qmc.Sobol(d=3, scramble=True, seed=seed).random(k)
    chosen, used = [], np.zeros(n, dtype=bool)
    for target in targets:
        distance = ((points - target) ** 2).sum(axis=1)
        distance[used] = np.inf
        best = int(np.argmin(distance))
        chosen.append(best)
        used[best] = True
    chosen = np.array(chosen)
    current = discrepancy(points[chosen])
    while time.monotonic() < deadline:
        trial = chosen.copy()
        position = rng.integers(k)
        replacement = rng.integers(n)
        if used[replacement]:
            continue
        trial[position] = replacement
        value = discrepancy(points[trial])
        if value < current:
            used[chosen[position]], used[replacement] = False, True
            chosen, current = trial, value
    return [int(i) for i in chosen]
