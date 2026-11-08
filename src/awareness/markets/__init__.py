"""Awareness Markets — the market-intelligence data layer behind showme.

Owns every text/event/sentiment data responsibility for the trade terminal:
quality-gated, sourced news (clustered into stories), financial sentiment,
economic and corporate calendars, holidays, SEC filings and market-sentiment
indicators. Everything is keyless by default and every record carries
provenance (:class:`~awareness.markets.models.ProviderRef`).

Design: ``docs/superpowers/specs/2026-11-08-markets-data-backbone-design.md``.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.3.0"
