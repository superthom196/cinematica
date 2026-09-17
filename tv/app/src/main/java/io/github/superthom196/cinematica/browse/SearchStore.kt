package io.github.superthom196.cinematica.browse

import io.github.superthom196.cinematica.api.CinematicaApi
import io.github.superthom196.cinematica.api.Movie
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** The empty state, verbatim from the phone page. */
const val SEARCH_HINT = "Search a title, a franchise (“james bond”), or a person."

data class SearchState(
    val query: String = "",
    val results: List<Movie> = emptyList(),
    val status: String = SEARCH_HINT,
    val searching: Boolean = false,
)

/**
 * Title / franchise / person search over the server's SSE endpoint.
 *
 * Results arrive one at a time as each candidate resolves — a cold franchise search takes ~25s for
 * the whole set but the first playable film lands in about one — so they are appended to the grid
 * as they come rather than waited for. A new search, or leaving the screen, bumps [searchGen] and
 * everything still in flight from the last one is discarded.
 */
class SearchStore(
    private val scope: CoroutineScope,
    private val api: CinematicaApi,
) {
    private val _state = MutableStateFlow(SearchState())
    val state: StateFlow<SearchState> = _state.asStateFlow()

    private var searchGen = 0
    private var job: Job? = null

    /** Entering the screen: a clean field and the hint, never the last search's leftovers. */
    fun open() {
        cancel()
        _state.value = SearchState()
    }

    fun setQuery(q: String) { _state.update { it.copy(query = q) } }

    fun cancel() {
        searchGen++
        job?.cancel()
        job = null
        _state.update { it.copy(searching = false) }
    }

    /** Fires on Go / Enter only — no debounce, and an empty query does nothing at all. */
    fun search(kind: String = "movie") {
        val q = _state.value.query.trim()
        if (q.isEmpty()) return
        val gen = ++searchGen
        job?.cancel()
        _state.value = SearchState(query = q, status = "Searching…", searching = true)
        job = scope.launch {
            var shown = 0
            var finished = false
            runCatching {
                api.searchStream(q, 30, kind).collect { ev ->
                    if (gen != searchGen) return@collect
                    when (ev) {
                        is CinematicaApi.SearchEvent.Found -> {
                            val n = ev.found.found ?: 0
                            _state.update { it.copy(status = "Checking $n candidate${plural(n)}…") }
                        }
                        is CinematicaApi.SearchEvent.Movie -> {
                            shown++
                            val n = shown
                            _state.update {
                                // Overlapping search hits would otherwise crash the lazy grid's keys.
                                val results = (it.results + ev.movie).distinctBy { m -> m.id ?: m }
                                it.copy(results = results, status = "$n found so far…")
                            }
                        }
                        is CinematicaApi.SearchEvent.Done -> {
                            finished = true
                            val d = ev.done
                            _state.update {
                                it.copy(searching = false, status = doneText(shown, d.found ?: 0, d.checked ?: 0, q))
                            }
                        }
                        is CinematicaApi.SearchEvent.Fail -> {
                            finished = true
                            _state.update { it.copy(searching = false, status = "Search failed.") }
                        }
                    }
                }
            }
            // The stream ending without a `done` is the transport giving up mid-search; the phone
            // page says the same thing, and only when nothing at all arrived.
            if (gen == searchGen && !finished) {
                _state.update {
                    it.copy(searching = false, status = if (shown == 0) "Search failed." else it.status)
                }
            }
        }
    }

    private fun plural(n: Int) = if (n == 1) "" else "s"

    private fun doneText(shown: Int, found: Int, checked: Int, q: String): String = when {
        shown > 0 ->
            "$shown playable result${plural(shown)}" +
                if (checked > shown) " · checked $checked of $found" else ""
        found > 0 ->
            "Found $found title${plural(found)} for “$q”, but none have a playable stream."
        else -> "Nothing found for “$q”."
    }
}
