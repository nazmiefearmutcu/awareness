"""Static checks: SPA empty-search diagnostics surface mode/corpus/phrase."""

from pathlib import Path


APP_JS = Path("src/awareness/api/web/app.js")
STYLE_CSS = Path("src/awareness/api/web/style.css")


def test_spa_empty_diagnostics_shows_mode_corpus_and_phrase_fallback() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")

    assert "function renderCapsDiagnostics(data, isSearch)" in app_js
    # Meta line mirrors CLI empty-state (mode / corpus / window).
    assert "mode=" in app_js and "formatSearchModeLabel(modeUsed" in app_js
    assert "corpus=" in app_js
    assert "window=" in app_js
    assert "caps-diagnostics-meta" in app_js
    # Phrase mode must remain informative even if hints are empty.
    assert 'modeUsed.toLowerCase() === "phrase"' in app_js
    assert "No exact phrase matches" in app_js
    # Do not hide the panel solely because hints[] is empty when meta exists.
    assert "if (!metaParts.length && !hints.length)" in app_js


def test_spa_diagnostics_meta_style_present() -> None:
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".caps-diagnostics-meta" in css


def test_spa_search_domain_facets_chips() -> None:
    """SPA renders domain facet chips under the search box when present."""
    app_js = APP_JS.read_text(encoding="utf-8")
    html = Path("src/awareness/api/web/index.html").read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert 'id="caps-facets"' in html
    assert "function renderCapsFacets(data, isSearch)" in app_js
    assert "facets.domains" in app_js or "data.facets" in app_js
    assert "facet-chip" in app_js
    assert ".caps-facets" in css
    assert ".facet-chip" in css


def test_spa_search_source_facets_chips() -> None:
    """SPA renders source facet chips when facets.sources is present."""
    app_js = APP_JS.read_text(encoding="utf-8")
    html = Path("src/awareness/api/web/index.html").read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert "function renderCapsFacets(data, isSearch)" in app_js
    assert "facets.sources" in app_js or "data.facets.sources" in app_js
    assert "facet-chip-source" in app_js
    assert "source_type" in app_js
    assert "caps-source" in app_js
    assert "sources" in html
    assert ".facet-chip-source" in css


def test_spa_search_language_facets_chips() -> None:
    """SPA renders language facet chips and wires language filter to search."""
    app_js = APP_JS.read_text(encoding="utf-8")
    html = Path("src/awareness/api/web/index.html").read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert 'id="caps-language"' in html
    assert "facets.languages" in app_js or "facets.languages" in app_js
    assert "facet-chip-lang" in app_js
    assert 'data-facet-kind": "language"' in app_js or 'data-facet-kind"] === "language"' in app_js or 'kind === "language"' in app_js
    assert 'params.set("language"' in app_js
    assert "caps-language" in app_js
    assert ".facet-chip-lang" in css
    assert 'data-facet-kind="language"' in css or "language" in css



def test_spa_domain_chip_syncs_selected_state() -> None:
    """Domain facet chip click must update selected highlight immediately."""
    app_js = APP_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert "function syncFacetChipSelection()" in app_js
    assert 'data-facet-kind": "domain"' in app_js or "data-facet-kind" in app_js
    assert "data-facet-value" in app_js
    assert "aria-pressed" in app_js
    # Optimistic highlight before search round-trip.
    assert "syncFacetChipSelection()" in app_js
    assert "void loadCaptures(true)" in app_js
    # Do not wipe chips at the start of a reload (selected state would flash off).
    # loadCaptures should call syncFacetChipSelection while loading, not renderCapsFacets(null…).
    assert "Keep facet chips visible during reload" in app_js or "syncFacetChipSelection();" in app_js
    assert ".facet-chip.is-active" in css


def test_spa_empty_diagnostics_shows_domain_filter() -> None:
    """Empty-search meta line includes active domain/source filters when present."""
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "diag.filters" in app_js
    assert 'metaParts.push("domain=" + domainFilter)' in app_js
    assert 'metaParts.push("source=" + sourceFilter)' in app_js
    assert 'metaParts.push("language=" + languageFilter)' in app_js


def test_spa_search_meta_shows_recency_boost_when_nonzero() -> None:
    """Search meta line appends recency=W when payload.recency_boost > 0."""
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "data.recency_boost" in app_js
    assert "recency=" in app_js
    assert "Number.isFinite(rb) && rb > 0" in app_js
