"""Observability: logging, metrics, structured event emission."""

from awareness.obs.logging import configure_logging, get_logger
from awareness.obs.metrics import MetricsRegistry, get_metrics

__all__ = ["MetricsRegistry", "configure_logging", "get_logger", "get_metrics"]
