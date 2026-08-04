"""``_sparkline`` extreme-pin correctness (cli/main.py).

The old pin wrote the min/max into ``sampled[0]``/``sampled[-1]``
unconditionally:

* upsampling (n < width) smeared the true peak into the LAST column even
  though the endpoints are already exact there, and
* downsampling (n > width) pinned the extremes to the wrong columns.

Fixed: pin only when downsampling, into the lattice column nearest the
extreme's true index. Non-finite values (NaN / inf) are dropped up front.
"""

from __future__ import annotations

from awareness.cli.main import _sparkline

_BLOCKS = "▁▂▃▄▅▆▇█"


def _max_column(spark: str) -> int:
    """Column (0-based) of the tallest block (ties → earliest column)."""
    return max(range(len(spark)), key=lambda i: (spark[i], -i))


def _min_column(spark: str) -> int:
    """Column (0-based) of the shortest block (ties → earliest column)."""
    return min(range(len(spark)), key=lambda i: (spark[i], -i))


def test_upsample_peak_stays_near_true_position_not_last() -> None:
    """[1, 5, 2, 1] upsampled to 20 columns: the peak (index 1 of 4) must
    land at column ~6 (19 * 1 / 3), NOT the last column."""
    spark = _sparkline([1, 5, 2, 1], width=20)
    assert len(spark) == 20
    col = _max_column(spark)
    assert 4 <= col <= 9, f"upsampled peak pinned to wrong column: {col}"
    assert col != 19


def test_downsample_spike_pinned_near_true_index() -> None:
    """200 samples with a spike at index 100, width 40: the peak must render
    at the lattice column nearest 39 * 100 / 199 ≈ 19.6, not the last."""
    values = [1.0] * 200
    values[100] = 10.0
    spark = _sparkline(values, width=40)
    assert len(spark) == 40
    col = _max_column(spark)
    assert 17 <= col <= 23, f"downsampled spike pinned to wrong column: {col}"
    assert col != 39


def test_downsample_minimum_pinned_near_true_index() -> None:
    """200 samples with a dip at index 50, width 40: the minimum lands at the
    lattice column nearest 39 * 50 / 199 ≈ 9.8."""
    values = [5.0] * 200
    values[50] = 0.0
    spark = _sparkline(values, width=40)
    assert len(spark) == 40
    col = _min_column(spark)
    assert 8 <= col <= 12, f"downsampled minimum pinned to wrong column: {col}"


def test_nan_and_inf_values_dropped_without_crash() -> None:
    spark = _sparkline([1.0, float("nan"), 2.0, float("inf"), 3.0], width=20)
    assert len(spark) == 20
    assert all(ch in _BLOCKS for ch in spark)


def test_all_nan_renders_empty() -> None:
    assert _sparkline([float("nan"), float("nan")]) == ""
