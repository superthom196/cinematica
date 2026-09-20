package io.github.superthom196.cinematica.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import io.github.superthom196.cinematica.AppViewModel
import io.github.superthom196.cinematica.UiState
import io.github.superthom196.cinematica.api.Episode
import io.github.superthom196.cinematica.api.Movie
import io.github.superthom196.cinematica.api.Shelf
import io.github.superthom196.cinematica.api.ShelfSnap
import io.github.superthom196.cinematica.api.StreamInfo
import io.github.superthom196.cinematica.api.TvDetail
import io.github.superthom196.cinematica.browse.PlayTarget
import kotlinx.coroutines.launch

/**
 * A series' detail page. Same shell as [DetailScreen] — hero, title, chips, overview — then two
 * panes: seasons and episodes on the left, and on the right the chosen episode's own page (still,
 * name, air date, overview, Play), which is what OK on an episode row opens. A series has no
 * single stream until an episode is chosen, so that pane is where the Play button lives.
 *
 * Everything is sized to a 1080p panel (540dp): the panes take what is left after the fixed rows
 * rather than a fixed height. (The first shape of this screen stacked a 150dp list and a Play
 * button under a 200dp top margin, which put the button below the bottom edge of the screen.)
 */
@Composable
fun SeriesDetailScreen(vm: AppViewModel, ui: UiState, movie: Movie) {
    val id = movie.id
    val playJob by vm.play.job.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()

    var detail by remember(id) { mutableStateOf<TvDetail?>(null) }
    // Where the series was left, when the server knows. The grid's copy is the fresher one — it is
    // patched on the way back from the player — so the detail response only fills a gap.
    var shelf by remember(id) { mutableStateOf(movie.shelf) }
    LaunchedEffect(id) {
        if (id != null) detail = vm.tvDetail(id).getOrNull()
        if (movie.shelf == null) detail?.shelf?.let { shelf = it }
    }

    var season by remember(id) { mutableStateOf(movie.shelf?.next?.s ?: 1) }
    var episodes by remember(id, season) { mutableStateOf<List<Episode>>(emptyList()) }
    LaunchedEffect(id, season) {
        episodes = if (id != null) vm.tvSeason(id, season).getOrNull()?.episodes.orEmpty() else emptyList()
    }

    var selected by remember(id, season) { mutableStateOf<Episode?>(null) }
    var stream by remember(id, season, selected) { mutableStateOf<StreamInfo?>(null) }
    var probe by remember(id, season, selected) { mutableStateOf(StreamProbe.Pending) }
    // Bumped by OK on an episode whose last lookup failed or found nothing: look again.
    var attempt by remember(id, season) { mutableStateOf(0) }

    LaunchedEffect(id, season, selected, attempt) {
        val ep = selected
        val e = ep?.episode
        if (id == null || ep == null || e == null) return@LaunchedEffect
        val s = ep.season ?: season
        val known = vm.cachedStream("tv:$id:$s:$e")
        if (known != null) { stream = known; probe = StreamProbe.Answered; return@LaunchedEffect }
        probe = StreamProbe.Pending
        vm.fetchStreamTv(id, s, e)
            .onSuccess { stream = it; probe = StreamProbe.Answered }
            .onFailure { probe = StreamProbe.Unreachable }
    }

    // Focus lands on the first episode once the list exists — not at composition, when it is
    // still empty and the request would go nowhere.
    val firstEpisodeFocus = remember { FocusRequester() }
    var focusedOnce by remember(id) { mutableStateOf(false) }
    LaunchedEffect(episodes) {
        if (focusedOnce || episodes.isEmpty()) return@LaunchedEffect
        focusedOnce = true
        runCatching { firstEpisodeFocus.requestFocus() }
    }

    // The episode the series is up to is chosen for the viewer, so Play is already on the right
    // one. Once only: choosing a different episode afterwards must stick.
    var resumed by remember(id) { mutableStateOf(false) }
    LaunchedEffect(episodes, shelf) {
        val next = shelf?.next
        if (resumed || next == null) return@LaunchedEffect
        // A shelf that only arrives with the detail response can name another season: open that
        // one and let its episodes bring this back round.
        if (next.s != season) { season = next.s; return@LaunchedEffect }
        if (episodes.isEmpty()) return@LaunchedEffect
        resumed = true
        episodes.firstOrNull { it.episode == next.e }?.let { selected = it }
    }

    // OK on a row opens the episode, and focus goes to its Play button — as opening a film lands
    // on Play. The button is composed on the frame after `selected` changes, hence the wait.
    val playFocus = remember { FocusRequester() }
    LaunchedEffect(selected) {
        if (selected == null) return@LaunchedEffect
        withFrameNanos { }
        runCatching { playFocus.requestFocus() }
    }

    // Back out of the buffering overlay stands the job down; Back anywhere else returns to the grid.
    BackHandler {
        if (playJob != null) vm.play.cancel() else vm.backFromDetail()
    }

    Box(Modifier.fillMaxSize()) {
        val hero = detail?.backdrop ?: movie.backdrop ?: detail?.poster ?: movie.poster
        Box(Modifier.fillMaxWidth().height(430.dp)) {
            PosterImage(hero, Modifier.fillMaxSize(), corner = 0.dp)
            Box(
                Modifier.fillMaxSize().background(
                    Brush.verticalGradient(
                        0f to Color.Transparent,
                        0.55f to CinematicaColors.Background.copy(alpha = 0.75f),
                        1f to CinematicaColors.Background,
                    ),
                ),
            )
        }
        Column(Modifier.fillMaxSize().padding(start = 40.dp, end = 40.dp, top = 130.dp, bottom = 20.dp)) {
            Text(
                detail?.title ?: movie.title.orEmpty(),
                style = MaterialTheme.typography.headlineMedium,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.fillMaxWidth(0.72f),
            )
            VSpace(6.dp)
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                seriesChips(movie, detail).forEach { InfoChip(it) }
            }
            VSpace(6.dp)
            Text(
                detail?.overview?.takeIf { it.isNotBlank() } ?: movie.overview.orEmpty(),
                style = MaterialTheme.typography.bodyMedium,
                color = CinematicaColors.Text,
                maxLines = 2, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.fillMaxWidth(0.72f),
            )
            VSpace(10.dp)
            Row(Modifier.fillMaxSize(), horizontalArrangement = Arrangement.spacedBy(24.dp)) {
                // ---- left: seasons and episodes ----------------------------------------
                Column(Modifier.fillMaxWidth(0.42f).fillMaxHeight()) {
                    Row(
                        Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        detail?.seasons.orEmpty().forEach { s ->
                            val n = s.n ?: return@forEach
                            val on = n == season
                            FocusSurface(
                                onClick = { if (!on) { season = n; selected = null } },
                                shape = RoundedCornerShape(50),
                                container = if (on) CinematicaColors.Accent else CinematicaColors.Surface,
                                focusedContainer = if (on) CinematicaColors.AccentBright else CinematicaColors.SurfaceHigh,
                            ) {
                                Text(
                                    s.name?.takeIf { it.isNotBlank() } ?: "Season $n",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = if (on) CinematicaColors.OnAccent else CinematicaColors.Text,
                                    modifier = Modifier.padding(horizontal = 14.dp, vertical = 8.dp),
                                )
                            }
                        }
                    }
                    VSpace(8.dp)
                    LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        itemsIndexed(episodes, key = { _, ep -> ep.episode ?: -1 }) { idx, ep ->
                            val on = selected == ep
                            FocusSurface(
                                onClick = {
                                    if (on && stream?.pick == null && probe != StreamProbe.Pending) attempt++
                                    selected = ep
                                },
                                modifier = Modifier.fillMaxWidth()
                                    .then(if (idx == 0) Modifier.focusRequester(firstEpisodeFocus) else Modifier),
                                container = if (on) CinematicaColors.SurfaceHigh else CinematicaColors.Surface,
                                onLongClick = {
                                    val e = ep.episode
                                    if (id != null && e != null) {
                                        val watched = !ep.watched
                                        episodes = episodes.map { if (it.episode == e) it.copy(watched = watched) else it }
                                        if (selected?.episode == e) selected = episodes.firstOrNull { it.episode == e }
                                        vm.setEpisodeWatched(id, ep.season ?: season, e, watched)
                                    }
                                },
                            ) {
                                Column(Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 8.dp)) {
                                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                                        Text(
                                            "E%02d · %s".format(ep.episode ?: (idx + 1), ep.name.orEmpty()),
                                            style = MaterialTheme.typography.bodyMedium.copy(
                                                fontWeight = if (on) FontWeight.SemiBold else FontWeight.Normal,
                                            ),
                                            // Seen episodes step back so the one to watch next stands out.
                                            color = when {
                                                on -> CinematicaColors.AccentBright
                                                ep.watched -> CinematicaColors.Muted
                                                else -> CinematicaColors.Text
                                            },
                                            maxLines = 1, overflow = TextOverflow.Ellipsis,
                                            modifier = Modifier.weight(1f),
                                        )
                                        ep.runtime?.takeIf { it > 0 }?.let {
                                            Text("$it min", style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted)
                                        }
                                    }
                                    ep.progress?.let {
                                        VSpace(4.dp)
                                        ProgressBar(it * 100.0, Modifier.fillMaxWidth(), height = 2.dp)
                                    }
                                }
                            }
                        }
                    }
                }
                // ---- right: the chosen episode ---------------------------------------
                val ep = selected
                if (ep == null) {
                    Box(Modifier.weight(1f).fillMaxHeight(), contentAlignment = Alignment.Center) {
                        Text("Choose an episode", style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Muted)
                    }
                } else {
                    EpisodePane(
                        ep = ep,
                        season = season,
                        stream = stream,
                        probe = probe,
                        busy = playJob != null,
                        playing = ui.playTitle != null,
                        playFocus = playFocus,
                        onPlay = { t ->
                            val e = ep.episode ?: return@EpisodePane
                            if (id == null || stream?.pick == null || playJob != null) return@EpisodePane
                            val s = ep.season ?: season
                            val label = "%s · S%02dE%02d · %s".format(detail?.title ?: movie.title.orEmpty(), s, e, ep.name.orEmpty())
                            scope.launch {
                                val auto = vm.currentAutoplayNext()
                                vm.play.play(PlayTarget("/api/play/tv/$id/$s/$e?autoplay=${if (auto) 1 else 0}", "tv:$id:$s:$e", label, t))
                            }
                        },
                        onStop = { vm.stopPlayback() },
                        fav = shelf?.fav == true,
                        onFav = {
                            if (id != null) {
                                val favOn = shelf?.fav != true
                                shelf = (shelf ?: Shelf()).copy(fav = favOn)
                                vm.setFav(id, favOn, seriesSnap(movie, detail))
                            }
                        },
                        modifier = Modifier.weight(1f).fillMaxHeight(),
                    )
                }
            }
        }
        playJob?.let { BufferingOverlay(it) }
    }
}

/** One episode's page: still and facts up top, its overview, how the stream lookup went, Play. */
@Composable
private fun EpisodePane(
    ep: Episode,
    season: Int,
    stream: StreamInfo?,
    probe: StreamProbe,
    busy: Boolean,
    playing: Boolean,
    playFocus: FocusRequester,
    /** Called with where to start, in seconds, or null for the beginning. */
    onPlay: (Int?) -> Unit,
    onStop: () -> Unit,
    fav: Boolean,
    onFav: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier) {
        Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            PosterImage(ep.still, Modifier.width(168.dp).aspectRatio(16f / 9f), corner = 8.dp)
            Column(Modifier.weight(1f)) {
                Text(
                    "S%02dE%02d".format(ep.season ?: season, ep.episode ?: 0),
                    style = MaterialTheme.typography.labelMedium,
                    color = CinematicaColors.AccentBright,
                )
                Text(
                    ep.name.orEmpty(),
                    style = MaterialTheme.typography.titleLarge,
                    maxLines = 2, overflow = TextOverflow.Ellipsis,
                )
                VSpace(6.dp)
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    episodeChips(ep).forEach { InfoChip(it) }
                }
            }
        }
        VSpace(8.dp)
        Text(
            ep.overview?.takeIf { it.isNotBlank() } ?: "No description.",
            style = MaterialTheme.typography.bodyMedium,
            color = if (ep.overview.isNullOrBlank()) CinematicaColors.Muted else CinematicaColors.Text,
            maxLines = 3, overflow = TextOverflow.Ellipsis,
        )
        VSpace(8.dp)
        // Pending is the one state StreamLine has nothing to say about, and here it is worth a
        // word: the Play button is grey for as long as the provider takes to answer.
        if (probe == StreamProbe.Pending) {
            Text("Finding a stream…", style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Muted)
        } else {
            StreamLine(stream, probe)
        }
        VSpace(8.dp)
        val pick = stream?.pick
        val resume = ep.resume_s
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
            PillButton(
                // Which episode and how far in, both, because this screen can be two episodes
                // away from the one the viewer actually stopped in the middle of.
                if (resume != null) {
                    "Resume S%d E%d · %s".format(ep.season ?: season, ep.episode ?: 0, formatTime(resume.toDouble()))
                } else "Play",
                onClick = { onPlay(resume) },
                primary = true,
                enabled = pick != null && !busy,
                modifier = Modifier.focusRequester(playFocus),
            )
            if (resume != null) {
                PillButton("From the start", onClick = { onPlay(null) }, enabled = pick != null && !busy)
            }
            PillButton("♥", onClick = onFav, primary = fav)
            // Only worth offering while this app actually has something open.
            if (playing) PillButton("Stop", onClick = onStop)
        }
    }
}

/** The series' own snapshot for the favourites wall. See [snapOf], which does this for a film. */
private fun seriesSnap(movie: Movie, detail: TvDetail?): ShelfSnap = ShelfSnap(
    kind = "tv",
    title = detail?.title ?: movie.title,
    year = detail?.firstAir?.take(4) ?: movie.year,
    poster = detail?.poster ?: movie.poster,
    imdb_id = detail?.imdbId ?: movie.imdb?.id,
)

/** Air date, runtime and rating — the episode's own facts row. */
private fun episodeChips(ep: Episode): List<String> = buildList {
    ep.air?.takeIf { it.isNotBlank() }?.let { add(it) }
    ep.runtime?.takeIf { it > 0 }?.let { add("$it min") }
    ep.vote?.takeIf { it > 0.0 }?.let { add("★ %.1f".format(it)) }
}

/** Years, status, episode runtime, rating and genres — the series equivalent of DetailScreen's chips. */
private fun seriesChips(movie: Movie, detail: TvDetail?): List<String> {
    val vote = detail?.vote
    val votes = detail?.votes
    return buildList {
        val first = detail?.firstAir?.take(4)?.takeIf { it.isNotBlank() }
        val last = detail?.lastAir?.take(4)?.takeIf { it.isNotBlank() }
        when {
            first != null && last != null && first != last -> add("$first–$last")
            first != null -> add(first)
        }
        detail?.status?.takeIf { it.isNotBlank() }?.let { add(it) }
        detail?.runtime?.takeIf { it > 0 }?.let { add("$it min") }
        if (vote != null && vote > 0.0) add("★ %.1f".format(vote) + if (votes != null) " ($votes)" else "")
        detail?.genres.orEmpty().forEach { add(it) }
    }
}
