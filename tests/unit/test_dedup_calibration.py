from __future__ import annotations

from awareness.dedup.calibration import calibrate_threshold, fpr_at_threshold


def test_fpr_endpoints() -> None:
    assert fpr_at_threshold(128, 128) == 1.0
    assert fpr_at_threshold(128, -1) == 0.0
    assert 0.0 < fpr_at_threshold(128, 50) < 0.5


def test_fpr_is_monotonic() -> None:
    prev = -1.0
    for t in range(0, 65, 8):
        cur = fpr_at_threshold(128, t)
        assert cur >= prev
        prev = cur


def test_calibrate_threshold_respects_target() -> None:
    target = 1e-6
    t = calibrate_threshold(128, target)
    assert fpr_at_threshold(128, t) <= target
    assert fpr_at_threshold(128, t + 1) > target
    assert 24 < t < 64


def test_tighter_target_gives_lower_threshold() -> None:
    assert calibrate_threshold(128, 1e-9) <= calibrate_threshold(128, 1e-3)
