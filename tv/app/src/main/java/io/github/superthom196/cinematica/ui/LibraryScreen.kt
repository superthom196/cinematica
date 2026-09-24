package io.github.superthom196.cinematica.ui

import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.focusRestorer
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import io.github.superthom196.cinematica.AppViewModel
import io.github.superthom196.cinematica.UiState
import io.github.superthom196.cinematica.api.ViewProgress
import io.github.superthom196.cinematica.browse.LibraryStore
import io.github.superthom196.cinematica.browse.sortName
import io.github.superthom196.cinematica.channelsReady
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

/** Slack around the grid so a focused tile's ring is not clipped as it grows. */
private val RING_ROOM = 6.dp

@Composable
fun LibraryScreen(vm: AppViewModel, ui: UiState) {
    val state by vm.library.state.collectAsStateWithLifecycle()
    val netState by vm.net.state.collectAsStateWithLifecycle()
    val playJob by vm.play.job.collectAsStateWithLifecycle()

    var sortOpen by remember { mutableStateOf(false) }
    var genreOpen by remember { mutableStateOf(false) }
    var netOpen by remember { mutableStateOf(false) }

    val gridState = rememberLazyGridState()
    val scope = rememberCoroutineScope()
    val headerFocus = remember { FocusRequester() }
    val itemFocus = remember { FocusRequester() }

    // Channels adds a second row below the followed list — the discovery ("Popular") tiles — with
    // a full-width label between them that is not itself a focus target. Every index below counts
    // across both rows as one conceptual list, so focus restore and the paging watch keep working
    // the same way they always have for every other kind, where popularCount is always 0.
    val followedCount = state.movies.size
    val popularCount = if (state.kind == LibraryStore.KIND_CHANNEL) state.popular.size else 0
    val showPopularLabel = popularCount > 0
    val focusableCount = followedCount + popularCount
    fun gridIndexOf(flat: Int): Int = if (flat < followedCount) flat else flat + (if (showPopularLabel) 1 else 0)

    // Coming back from a film or a search: the grid returns to the tile that was left, rather than
    // to the top with focus on the header.
    val restoreTo = remember { vm.library.focusIndex }
    var restored by remember { mutableStateOf(false) }
    val focusTarget = restoreTo.coerceIn(0, (focusableCount - 1).coerceAtLeast(0))
    LaunchedEffect(followedCount, popularCount) {
        if (restored || focusableCount == 0) return@LaunchedEffect
        restored = true
        val gridTarget = gridIndexOf(focusTarget)
        gridState.scrollToItem(gridTarget)
        snapshotFlow { gridState.layoutInfo.visibleItemsInfo.any { it.index == gridTarget } }.first { it }
        withFrameNanos { }
        runCatching { itemFocus.requestFocus() }
    }

    // A new view (sort, genres or kind) empties the list, but the grid keeps whatever offset it
    // had, which left the first row of the fresh view sliced off at the top of the screen.
    LaunchedEffect(focusableCount == 0) { if (focusableCount == 0) gridState.scrollToItem(0) }

    // How far down the grid has got. This — and nothing on a timer — is what asks for more films.
    val lastVisible by remember {
        derivedStateOf { gridState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0 }
    }
    LaunchedEffect(lastVisible, state.movies.size) { vm.library.onVisible(lastVisible) }

    // Back from anywhere in the grid goes to the menu bar, like MATV; only Back from the menu bar
    // itself leaves the app. Holding Up through every row was no way to get there, and leaving the
    // app from the first row by mistake was worse.
    var inHeader by remember { mutableStateOf(false) }
    BackHandler(enabled = !inHeader) {
        scope.launch { gridState.scrollToItem(0) }
        runCatching { headerFocus.requestFocus() }
    }

    if (sortOpen) {
        SortDialog(state.sort, onPick = { vm.library.setSort(it) }, onDismiss = { sortOpen = false })
    }
    if (genreOpen) {
        GenreDialog(
            genres = state.genres,
            include = state.include,
            exclude = state.exclude,
            onToggle = { inc, exc -> vm.library.setGenres(inc, exc) },
            onApply = { vm.library.reload() },
            onClear = { vm.library.setGenres(emptySet(), emptySet()); vm.library.reload() },
            onDismiss = { genreOpen = false },
        )
    }
    if (netOpen) {
        NetCheckDialog(netState.net, onRemeasure = { vm.net.remeasure() }, onDismiss = { netOpen = false })
    }

    Column(Modifier.fillMaxSize().padding(start = 18.dp, end = 18.dp, top = 10.dp)) {
        Row(
            Modifier.fillMaxWidth().onFocusChanged { inHeader = it.hasFocus },
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Wordmark(logoSize = 26.dp)
            Spacer(Modifier.weight(1f))
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                // The watchlist and the channels list are both a third/fourth view of the same
                // grid, not screens of their own: they belong in the switch that already chooses
                // which wall you are looking at. Channels only shows once the server has a
                // channels-capable provider ready — or the wall is already on it, so switching
                // away doesn't strand the segment mid-visit.
                val noPool = state.kind == LibraryStore.KIND_FAV || state.kind == LibraryStore.KIND_CHANNEL
                val segments = buildList {
                    add("movie" to "Movies")
                    add("tv" to "Series")
                    if (ui.channelsReady || state.kind == LibraryStore.KIND_CHANNEL) add(LibraryStore.KIND_CHANNEL to "Channels")
                    add(LibraryStore.KIND_FAV to "♥")
                }
                SegmentedPill(
                    options = segments,
                    selected = state.kind,
                    onSelect = { vm.library.setKind(it) },
                )
                PillButton("Search", onClick = { vm.openSearch() }, dense = true, modifier = Modifier.focusRequester(headerFocus))
                // Sort and genres belong to a pool. The wall is already in the order it wants.
                PillButton("Sort: ${sortName(state.sort)} ▾", onClick = { sortOpen = true }, dense = true, enabled = !noPool)
                val n = state.include.size + state.exclude.size
                PillButton("Genres ${if (n > 0) "($n) " else ""}▾", onClick = { genreOpen = true }, dense = true, enabled = !noPool)
                PillButton(vm.net.chipLabel(netState.net), onClick = { vm.net.refresh(); netOpen = true }, dense = true)
                PillButton("Settings", onClick = { vm.openSettings() }, dense = true)
            }
        }
        VSpace(8.dp)
        // A cold view assembles its whole pool before the first row can exist, which takes minutes
        // for a biased film wall. A blank grid for that long reads as a broken service, so the
        // server's own progress goes on screen as a ring that only ever closes.
        // Never on the watchlist or the channels list: neither is a pool being assembled — one is
        // a file read, the other a single request the server has already ranked.
        val gathering = state.movies.isEmpty() && state.loading && !state.failed &&
            state.kind != LibraryStore.KIND_FAV && state.kind != LibraryStore.KIND_CHANNEL
        Box(Modifier.fillMaxSize()) {
        if (gathering) GatheringRing(state.progress, state.kind)
        LazyVerticalGrid(
            state = gridState,
            columns = GridCells.Fixed(LibraryStore.COLUMNS),
            horizontalArrangement = Arrangement.spacedBy(2.dp),
            verticalArrangement = Arrangement.spacedBy(2.dp),
            contentPadding = PaddingValues(start = RING_ROOM, end = RING_ROOM, top = RING_ROOM, bottom = 40.dp),
            modifier = Modifier.fillMaxSize().focusRestorer(),
        ) {
            itemsIndexed(state.movies, key = { i, m -> m.id ?: "row-$i" }) { index, movie ->
                MovieTile(
                    movie,
                    onClick = { vm.library.rememberFocus(index); vm.openDetail(movie) },
                    progress = playJob?.takeIf { it.id == movie.id }?.pct,
                    // A channel tile has no watched state of its own — follow/unfollow lives on
                    // its detail screen, not behind a long-press on the wall.
                    onLongClick = if (state.kind == LibraryStore.KIND_CHANNEL) null else ({ vm.toggleWatched(movie) }),
                    modifier = Modifier
                        .onFocusChanged { if (it.isFocused) vm.library.rememberFocus(index) }
                        .then(if (index == focusTarget) Modifier.focusRequester(itemFocus) else Modifier),
                )
            }
            if (showPopularLabel) {
                item(span = { GridItemSpan(maxLineSpan) }, key = "popular-label") {
                    Box(Modifier.fillMaxWidth().padding(top = 10.dp, bottom = 4.dp)) {
                        Text(
                            "Popular",
                            style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
                            color = CinematicaColors.Muted,
                        )
                    }
                }
                itemsIndexed(state.popular, key = { i, m -> "popular-" + (m.id ?: "row-$i") }) { j, movie ->
                    val flat = followedCount + j
                    MovieTile(
                        movie,
                        onClick = { vm.library.rememberFocus(flat); vm.openDetail(movie) },
                        onLongClick = null,
                        modifier = Modifier
                            .onFocusChanged { if (it.isFocused) vm.library.rememberFocus(flat) }
                            .then(if (flat == focusTarget) Modifier.focusRequester(itemFocus) else Modifier),
                    )
                }
            }
            item(span = { GridItemSpan(maxLineSpan) }, key = "footer") {
                val text = if (gathering) "" else footerText(state)
                if (text.isNotEmpty()) {
                    Box(Modifier.fillMaxWidth().padding(top = 10.dp), contentAlignment = Alignment.Center) {
                        Text(text, style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Muted)
                    }
                }
            }
        }
        }
    }
}

/**
 * The ring over an empty grid. Determinate once the server reports a fraction, closing smoothly
 * between polls; a slow spin while it has nothing yet, so the screen is never still. The words
 * are the server's own, so they follow what it is actually doing.
 */
@Composable
private fun GatheringRing(progress: ViewProgress?, kind: String) {
    val fraction = progress?.fraction?.coerceIn(0f, 1f) ?: 0f
    val sweep by animateFloatAsState(360f * fraction, tween(1_200, easing = LinearEasing), label = "sweep")
    val spin = rememberInfiniteTransition(label = "spin")
    val angle by spin.animateFloat(
        initialValue = 0f, targetValue = 360f,
        animationSpec = infiniteRepeatable(tween(2_400, easing = LinearEasing), RepeatMode.Restart),
        label = "angle",
    )
    val title = progress?.label?.takeIf { it.isNotBlank() } ?: "Gathering the hottest streams"
    val things = if (kind == "tv") "series" else "movies"
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Canvas(Modifier.size(64.dp).rotate(if (fraction > 0f) -90f else angle)) {
                drawArc(
                    color = CinematicaColors.Muted, startAngle = 0f, sweepAngle = 360f, useCenter = false,
                    size = Size(size.width, size.height), style = Stroke(width = 6f), alpha = 0.35f,
                )
                drawArc(
                    color = CinematicaColors.AccentBright, startAngle = 0f,
                    sweepAngle = if (fraction > 0f) sweep else 100f, useCenter = false,
                    size = Size(size.width, size.height), style = Stroke(width = 6f),
                )
            }
            VSpace(16.dp)
            Text(title, style = MaterialTheme.typography.titleMedium, color = CinematicaColors.Text)
            VSpace(6.dp)
            Text(
                "Ranking the best $things and checking each one really streams. A couple of minutes the first time, instant after that.",
                style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted,
            )
        }
    }
}

/** The one line under the grid, in the phone page's words. */
private fun footerText(state: io.github.superthom196.cinematica.browse.LibraryState): String = when {
    state.kind == LibraryStore.KIND_FAV ->
        if (state.movies.isEmpty() && !state.loading) "Nothing saved yet — open a title and press Save for later" else ""
    state.kind == LibraryStore.KIND_CHANNEL ->
        if (state.movies.isEmpty() && state.popular.isEmpty() && !state.loading && !state.failed) {
            if ("channels.search" in state.channelOps) "Search to find a channel and follow it"
            else "Paste a channel's link or @handle into Search to follow it"
        } else ""
    state.failed -> "Failed to load more."
    state.exhausted && state.movies.isEmpty() -> "Nothing playable found — try a different sort or fewer genres."
    state.exhausted -> "End of the list · ${state.movies.size} ${if (state.kind == "tv") "series" else "movies"}"
    state.movies.isEmpty() && state.loading -> "Finding ${if (state.kind == "tv") "series" else "movies"}… (checking what is actually streamable)"
    state.movies.isEmpty() -> "Finding ${if (state.kind == "tv") "series" else "movies"}…"
    else -> ""
}
