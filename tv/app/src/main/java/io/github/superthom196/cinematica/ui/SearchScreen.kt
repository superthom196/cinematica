package io.github.superthom196.cinematica.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.focusRestorer
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import io.github.superthom196.cinematica.AppViewModel
import io.github.superthom196.cinematica.browse.LibraryStore

/**
 * Title / franchise / person search, full screen.
 *
 * Nothing fires on a keystroke: `/api/search/stream` resolves real torrents and a search per letter
 * would be a denial of service on the Pi. Go (or Enter on the keyboard) starts it, results land in
 * the grid as each one resolves, and the status line says where the server has got to.
 */
@Composable
fun SearchScreen(vm: AppViewModel) {
    val state by vm.search.state.collectAsStateWithLifecycle()
    val playJob by vm.play.job.collectAsStateWithLifecycle()
    // Search only makes sense within whichever shelf the grid is currently showing — films or
    // series — so it goes to the server as the same kind the library is browsing.
    val libraryState by vm.library.state.collectAsStateWithLifecycle()
    val kind = libraryState.kind
    val field = remember { FocusRequester() }
    val gridState = rememberLazyGridState()
    LaunchedEffect(Unit) { runCatching { field.requestFocus() } }

    Column(Modifier.fillMaxSize().padding(start = 18.dp, end = 18.dp, top = 12.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("Search", style = MaterialTheme.typography.headlineSmall)
            HSpace(18.dp)
            TvTextField(
                state.query,
                { vm.search.setQuery(it) },
                placeholder = "Search for a movie…",
                imeAction = ImeAction.Search,
                onDone = { vm.search.search(kind = kind) },
                modifier = Modifier.width(560.dp).focusRequester(field),
            )
            HSpace(10.dp)
            PillButton("Go", onClick = { vm.search.search(kind = kind) }, primary = true, dense = true)
        }
        VSpace(8.dp)
        Text(state.status, style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Muted)
        VSpace(8.dp)
        LazyVerticalGrid(
            state = gridState,
            columns = GridCells.Fixed(LibraryStore.COLUMNS),
            horizontalArrangement = Arrangement.spacedBy(2.dp),
            verticalArrangement = Arrangement.spacedBy(2.dp),
            contentPadding = PaddingValues(start = 6.dp, end = 6.dp, top = 6.dp, bottom = 40.dp),
            modifier = Modifier.fillMaxSize().focusRestorer(),
        ) {
            items(state.results, key = { it.id ?: it.hashCode().toString() }) { movie ->
                MovieTile(
                    movie,
                    // The search result carries its own resolved stream, so the detail screen opens
                    // with Play already live instead of re-asking the server for the same pick.
                    onClick = { vm.openDetail(movie) },
                    progress = playJob?.takeIf { it.id == movie.id }?.pct,
                )
            }
        }
    }
}
