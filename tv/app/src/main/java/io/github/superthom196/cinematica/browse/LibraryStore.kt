package io.github.superthom196.cinematica.browse

import io.github.superthom196.cinematica.api.CinematicaApi
import io.github.superthom196.cinematica.api.Genre
import io.github.superthom196.cinematica.api.Movie
import io.github.superthom196.cinematica.api.Shelf
import io.github.superthom196.cinematica.api.ViewProgress
import io.github.superthom196.cinematica.data.Prefs
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/** The three sorts the server understands, with the phone page's wording for each. */
val SORTS: List<Triple<String, String, String>> = listOf(
    Triple("top", "Top rated", "Best IMDb score, all time"),
    Triple("balanced", "Balanced", "Rating plus a recency boost"),
    Triple("recent", "Recent", "Last 5 years, best rated first"),
)

fun sortName(key: String): String = SORTS.firstOrNull { it.first == key }?.second ?: "Top rated"

data class LibraryState(
    /** Films actually on the grid — always a whole number of rows unless the list is exhausted. */
    val movies: List<Movie> = emptyList(),
    val loading: Boolean = false,
    val exhausted: Boolean = false,
    /** The last `/api/movies` failed. Nothing retries on its own; moving focus on asks again. */
    val failed: Boolean = false,
    val sort: String = "top",
    /** "movie" or "tv" — which pool the grid, genres, and include/exclude sets belong to. */
    val kind: String = "movie",
    val include: Set<String> = emptySet(),
    val exclude: Set<String> = emptySet(),
    val genres: List<Genre> = emptyList(),
    /** The UK/US bias in Settings, for both kinds. See [Prefs.ukUsBias]. */
    val bias: Boolean = true,
    /** While the grid is empty and a request is out: what the server says it is doing. */
    val progress: ViewProgress? = null,
)

/**
 * The poster wall's paging.
 *
 * `/api/movies` resolves real torrents, so it is asked for exactly one grid row at a time and only
 * while focus is within [ROWS_AHEAD] rows of the end — the same shape as the phone page's
 * fetchRow/drip/pump, minus the scroll maths a lazy grid does for us. Every response has to prove
 * it still belongs to the current view ([loadGen]) before it is rendered: a sort or genre change
 * while a request is in flight would otherwise paint the old view's films into the new grid and
 * skip the new view's real first page.
 */
class LibraryStore(
    private val scope: CoroutineScope,
    private val api: CinematicaApi,
    private val prefs: Prefs,
) {
    companion object {
        /** Columns in the grid, and therefore the `limit` on every request. */
        const val COLUMNS = 6
        const val ROWS_AHEAD = 3

        /**
         * The third segment of the header switch: the favourites wall, films and series mixed.
         * It lives in [LibraryState.kind] like the other two, but is never persisted — a cold
         * start always lands on a pool, so a server with no shelf can never open on an empty wall.
         */
        const val KIND_FAV = "fav"
    }

    private val _state = MutableStateFlow(LibraryState())
    val state: StateFlow<LibraryState> = _state.asStateFlow()

    /** Fetched but not yet placed: a short row waits here rather than leaving a ragged gap. */
    private val pending = mutableListOf<Movie>()
    private var offset = 0
    private var loadGen = 0
    private var lastVisible = -1
    private var pumpJob: Job? = null

    /** Which tile to put focus back on when the grid returns from a detail or search screen. */
    var focusIndex: Int = 0
        private set

    fun rememberFocus(index: Int) { focusIndex = index }

    /** Restores the saved kind, sort and that kind's genre selection. Call before the first [pump]. */
    suspend fun restore() {
        val kind = prefs.kind.first()
        val sort = prefs.sort.first()
        val include = if (kind == "tv") prefs.genresIncludeTv.first() else prefs.genresInclude.first()
        val exclude = if (kind == "tv") prefs.genresExcludeTv.first() else prefs.genresExclude.first()
        val bias = prefs.ukUsBias.first()
        _state.update { it.copy(kind = kind, sort = sort, include = include, exclude = exclude, bias = bias) }
    }

    /**
     * The genre names, fetched once per kind. A failure leaves the menu empty on purpose — the grid
     * does not depend on it, and a hanging `/api/genres` must not hold up the first row of films.
     */
    fun loadGenres() {
        val kind = _state.value.kind
        // The favourites wall is not a pool and has no genre menu of its own.
        if (kind == KIND_FAV) return
        scope.launch {
            val resp = withTimeoutOrNull(20_000) { runCatching { api.genres(kind) }.getOrNull() }
            val genres = resp?.genres.orEmpty().filter { it.id != null && it.name != null }
            if (genres.isNotEmpty()) _state.update { it.copy(genres = genres) }
        }
    }

    /** The grid says how far it has got; that is the only thing that asks for more films. */
    fun onVisible(index: Int) {
        if (index > lastVisible) lastVisible = index
        pump()
    }

    fun setSort(sort: String) {
        if (sort == _state.value.sort) return
        _state.update { it.copy(sort = sort) }
        scope.launch { prefs.setSort(sort) }
        reload()
    }

    /**
     * Films vs series: persists the choice, swaps in that kind's saved genre selection, and starts
     * the view over — new genre list, empty grid, paging from the top.
     */
    fun setKind(k: String) {
        if (k == _state.value.kind) return
        scope.launch {
            val include = if (k == "tv") prefs.genresIncludeTv.first() else prefs.genresInclude.first()
            val exclude = if (k == "tv") prefs.genresExcludeTv.first() else prefs.genresExclude.first()
            // The wall is a place to visit, not a view to come back to: only a real pool is saved.
            if (k != KIND_FAV) prefs.setKind(k)
            _state.update { it.copy(kind = k, include = include, exclude = exclude, genres = emptyList()) }
            loadGenres()
            reload()
        }
    }

    /** The UK/US bias switch in Settings: persisted, and a brand-new view. */
    fun setBias(bias: Boolean) {
        if (bias == _state.value.bias) return
        _state.update { it.copy(bias = bias) }
        scope.launch { prefs.setUkUsBias(bias) }
        reload()
    }

    /** A genre toggle: persisted at once (so the header count follows), but no reload until Apply. */
    fun setGenres(include: Set<String>, exclude: Set<String>) {
        _state.update { it.copy(include = include, exclude = exclude) }
        val kind = _state.value.kind
        scope.launch {
            if (kind == "tv") {
                prefs.setGenresIncludeTv(include)
                prefs.setGenresExcludeTv(exclude)
            } else {
                prefs.setGenresInclude(include)
                prefs.setGenresExclude(exclude)
            }
        }
    }

    /** Apply / Clear in the genre menu, and any sort change: a brand-new view. */
    fun reload() {
        loadGen++
        pumpJob?.cancel()
        pumpJob = null
        pending.clear()
        offset = 0
        lastVisible = -1
        focusIndex = 0
        _state.update { it.copy(movies = emptyList(), loading = false, exhausted = false, failed = false) }
        pump()
    }

    fun pump() {
        if (pumpJob?.isActive == true) return
        if (_state.value.kind == KIND_FAV) { pumpShelf(); return }
        if (!needMore()) { drip(); return }
        val gen = loadGen
        pumpJob = scope.launch {
            while (isActive && gen == loadGen && needMore()) {
                _state.update { it.copy(loading = true, failed = false) }
                val s = _state.value
                // An empty grid gets the server's progress ring while its first row is on the way;
                // later rows arrive behind what is already on screen and need no such reassurance.
                val poll = if (s.movies.isEmpty() && pending.isEmpty()) launch {
                    while (isActive) {
                        val p = withTimeoutOrNull(5_000) { runCatching { api.progress(s.sort, s.include, s.exclude, s.kind, s.bias) }.getOrNull() }
                        if (p != null && gen == loadGen) _state.update { it.copy(progress = p) }
                        delay(1_500)
                    }
                } else null
                val resp = runCatching { api.movies(offset, COLUMNS, s.sort, s.include, s.exclude, s.kind, s.bias) }
                poll?.cancel()
                _state.update { it.copy(progress = null) }
                if (gen != loadGen) return@launch
                val value = resp.getOrNull()
                if (value == null) {
                    // Never retried on a timer: /api/movies is not a cheap list call. The next time
                    // focus moves further down the grid asks again, and nothing else does.
                    _state.update { it.copy(loading = false, failed = true) }
                    return@launch
                }
                val page = value.movies.orEmpty()
                // The pinned strip rides on the offset-0 response only, and goes in front of the
                // catalogue. Being first in `got` is also what makes the dedupe below drop the
                // catalogue's own copy of a pinned title rather than the other way round.
                val got = if (offset == 0) value.pinned + page else page
                // A film can repeat across pages when the pool re-sorts between requests, and a
                // repeated id crashes the lazy grid's keys: drop anything already shown or waiting.
                val seen = HashSet<String>()
                _state.value.movies.forEach { m -> m.id?.let(seen::add) }
                pending.forEach { m -> m.id?.let(seen::add) }
                pending += got.filter { m -> val id = m.id; id == null || seen.add(id) }
                // Paging is the catalogue's alone: the pinned strip is not part of the offset.
                offset += page.size
                _state.update { it.copy(loading = false, exhausted = value.more != true || got.isEmpty()) }
                drip()
            }
            if (gen == loadGen) { _state.update { it.copy(loading = false) }; drip() }
        }
    }

    /**
     * The favourites wall: one request for the lot, no paging, no progress ring — the server is
     * reading its own file, not resolving torrents. A server that has never heard of the route
     * answers 404, and an empty wall with its one quiet line is the right thing to show for that
     * as much as for a viewer who has not favourited anything yet.
     */
    private fun pumpShelf() {
        if (_state.value.exhausted) return
        val gen = loadGen
        pumpJob = scope.launch {
            _state.update { it.copy(loading = true, failed = false) }
            val items = runCatching { api.shelf() }.getOrNull()?.items.orEmpty()
            if (gen != loadGen) return@launch
            _state.update { it.copy(movies = items, loading = false, exhausted = true) }
        }
    }

    /**
     * A shelf change made elsewhere — the ♥ on a detail screen, a long-press on a tile. The grid's
     * own copy follows at once so the tile says what just happened without waiting for a page.
     */
    fun patchShelf(id: String, transform: (Shelf) -> Shelf) {
        _state.update { st ->
            st.copy(movies = st.movies.map { m -> if (m.id == id) m.copy(shelf = transform(m.shelf ?: Shelf())) else m })
        }
    }

    /**
     * Back from a film: ask for the first page again — the server has just served it, so it is
     * cheap — and let its fresh `shelf` objects land on the tiles already on screen, with anything
     * newly pinned going in front. Never re-pages and never empties the grid.
     */
    fun refreshShelf() {
        if (_state.value.kind == KIND_FAV) { reload(); return }
        val gen = loadGen
        scope.launch {
            val s = _state.value
            val page = runCatching { api.movies(0, COLUMNS, s.sort, s.include, s.exclude, s.kind, s.bias) }.getOrNull()
                ?: return@launch
            if (gen != loadGen) return@launch
            val fresh = HashMap<String, Shelf?>()
            (page.pinned + page.movies.orEmpty()).forEach { m -> m.id?.let { fresh[it] = m.shelf } }
            _state.update { st ->
                val patched = st.movies.map { m ->
                    val id = m.id
                    if (id != null && fresh.containsKey(id)) m.copy(shelf = fresh[id]) else m
                }
                val have = patched.mapNotNull { it.id }.toHashSet()
                st.copy(movies = page.pinned.filter { m -> m.id?.let { it !in have } == true } + patched)
            }
        }
    }

    private fun needMore(): Boolean {
        val s = _state.value
        if (s.exhausted) return false
        val have = s.movies.size + pending.size
        return have - (lastVisible + 1) < COLUMNS * ROWS_AHEAD
    }

    /** Moves whole rows onto the grid; a short row only lands when the list is genuinely finished. */
    private fun drip() {
        val exhausted = _state.value.exhausted
        val take = if (exhausted) pending.size else (pending.size / COLUMNS) * COLUMNS
        if (take <= 0) return
        val row = ArrayList<Movie>(take)
        repeat(take) { row += pending.removeAt(0) }
        _state.update { it.copy(movies = it.movies + row) }
    }
}
