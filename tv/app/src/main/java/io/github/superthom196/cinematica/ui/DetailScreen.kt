package io.github.superthom196.cinematica.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import io.github.superthom196.cinematica.AppViewModel
import io.github.superthom196.cinematica.UiState
import io.github.superthom196.cinematica.api.Movie
import io.github.superthom196.cinematica.api.MovieDetail
import io.github.superthom196.cinematica.api.Shelf
import io.github.superthom196.cinematica.api.ShelfSnap
import io.github.superthom196.cinematica.api.StreamInfo
import kotlinx.coroutines.launch

/** How the stream lookup went, which is not the same question as "is there a pick". Shared with
 *  SeriesDetailScreen, which asks the same question about an episode. */
enum class StreamProbe { Pending, Answered, Unreachable }

@Composable
fun DetailScreen(vm: AppViewModel, ui: UiState, movie: Movie) {
    if (movie.kind == "tv") { SeriesDetailScreen(vm, ui, movie); return }

    val id = movie.id
    val playJob by vm.play.job.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()

    var detail by remember(id) { mutableStateOf<MovieDetail?>(null) }
    var stream by remember(id) { mutableStateOf(id?.let { vm.cachedStream("m:$it") } ?: movie.stream) }
    var probe by remember(id) { mutableStateOf(if (stream != null) StreamProbe.Answered else StreamProbe.Pending) }

    // The grid's own copy is the fresher one — it is patched on the way back from the player — so
    // the detail response only fills this in when the grid knew nothing about the title.
    var shelf by remember(id) { mutableStateOf(movie.shelf) }

    LaunchedEffect(id) {
        if (id != null) detail = vm.movieDetail(id)
        if (movie.shelf == null) detail?.shelf?.let { shelf = it }
    }
    LaunchedEffect(id) {
        if (id == null || stream != null) return@LaunchedEffect
        vm.fetchStream(id)
            .onSuccess { stream = it; probe = StreamProbe.Answered }
            .onFailure { probe = StreamProbe.Unreachable }
    }

    val playFocus = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { playFocus.requestFocus() } }

    // Back out of the buffering overlay stands the job down; Back anywhere else returns to the grid.
    BackHandler {
        if (playJob != null) vm.play.cancel() else vm.backFromDetail()
    }

    Box(Modifier.fillMaxSize()) {
        val hero = movie.backdrop ?: movie.poster
        Box(Modifier.fillMaxWidth().height(430.dp)) {
            PosterImage(hero, Modifier.fillMaxSize(), corner = 0.dp)
            // The page has to carry on below the picture, so the picture has to end in the page.
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
        Column(Modifier.fillMaxSize().padding(start = 40.dp, end = 40.dp, top = 250.dp)) {
            Text(
                movie.title.orEmpty(),
                style = MaterialTheme.typography.headlineMedium,
                maxLines = 2, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.fillMaxWidth(0.72f),
            )
            val tagline = detail?.tagline.orEmpty()
            if (tagline.isNotBlank()) {
                Text(
                    tagline,
                    style = MaterialTheme.typography.bodyMedium.copy(fontStyle = FontStyle.Italic),
                    color = CinematicaColors.Muted, maxLines = 1, overflow = TextOverflow.Ellipsis,
                )
            }
            VSpace(8.dp)
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                chips(movie, detail).forEach { InfoChip(it) }
            }
            VSpace(10.dp)
            Text(
                detail?.overview?.takeIf { it.isNotBlank() } ?: movie.overview.orEmpty(),
                style = MaterialTheme.typography.bodyMedium,
                color = CinematicaColors.Text,
                maxLines = 4, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.fillMaxWidth(0.72f),
            )
            VSpace(12.dp)
            StreamLine(stream, probe)
            VSpace(12.dp)
            val pick = stream?.pick
            val busy = playJob != null
            val resume = shelf?.resume_s
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
                PillButton(
                    // Where the viewer actually got to, rather than a word for it: "Resume 47:12"
                    // is the whole reason to press this button instead of the one beside it.
                    if (resume != null) "Resume ${formatTime(resume.toDouble())}" else "Play",
                    onClick = { if (pick != null && !busy) vm.play.play(movie, resume) },
                    primary = true,
                    enabled = pick != null && !busy,
                    modifier = Modifier.focusRequester(playFocus),
                )
                if (resume != null) {
                    PillButton("From the start", onClick = { if (pick != null && !busy) vm.play.play(movie) }, enabled = pick != null && !busy)
                }
                PillButton(
                    saveLabel(shelf?.fav == true),
                    onClick = {
                        if (id != null) {
                            val on = shelf?.fav != true
                            shelf = (shelf ?: Shelf()).copy(fav = on)
                            vm.setFav(id, on, snapOf(movie, detail))
                        }
                    },
                    primary = shelf?.fav == true,
                )
                // Only worth offering while this app actually has a film open.
                if (ui.playTitle != null) {
                    PillButton("Stop", onClick = { vm.stopPlayback() })
                }
            }
        }
        playJob?.let { BufferingOverlay(it) }
    }
}

/**
 * The watchlist button, in words rather than a bare heart: a heart says "love", and this is
 * "keep it for later", which the header's ♥ segment then lists until it has been watched.
 */
fun saveLabel(saved: Boolean): String = if (saved) "♥ Saved" else "♥ Save for later"

/**
 * Enough of a title for the watchlist to draw it after the catalogue has moved on. The
 * server has nowhere else to get it from, so it rides along with every save.
 */
fun snapOf(movie: Movie, detail: MovieDetail?): ShelfSnap = ShelfSnap(
    kind = movie.kind ?: "movie",
    title = detail?.title ?: movie.title,
    year = detail?.year ?: movie.year,
    poster = detail?.poster ?: movie.poster,
    imdb_id = detail?.imdb_id ?: movie.imdb?.id,
)

/** Year, runtime, rating and genre names — from the grid's data first, then from `/api/movie`. */
private fun chips(movie: Movie, detail: MovieDetail?): List<String> {
    val vote = detail?.vote ?: movie.vote
    val votes = detail?.votes ?: movie.votes
    return buildList {
        (detail?.release?.take(4)?.takeIf { it.isNotBlank() } ?: movie.year)?.takeIf { it.isNotBlank() }?.let { add(it) }
        detail?.runtime?.takeIf { it > 0 }?.let { add("$it min") }
        if (vote != null && vote > 0.0) add("★ %.1f".format(vote) + if (votes != null) " ($votes)" else "")
        detail?.genres.orEmpty().forEach { add(it) }
    }
}

/**
 * Nothing at all when there is a pick — the stream details are plumbing. Everything else the server
 * said about why there isn't one, verbatim: "no usable stream" and a 403 are very different
 * answers, and collapsing them into "unavailable" is the phone UI's one acknowledged weakness.
 */
@Composable
fun StreamLine(stream: StreamInfo?, probe: StreamProbe) {
    val err = stream?.err
    val lines = buildList {
        when {
            probe == StreamProbe.Unreachable -> add("Could not check this one." to CinematicaColors.Danger)
            stream == null -> Unit
            stream.pick == null -> add("No playable stream for this one." to CinematicaColors.Danger)
        }
        if (!err.isNullOrBlank()) {
            add(err to CinematicaColors.Warn)
            // A 403 is not this film's fault: the provider is refusing this server's address, so everything is degraded.
            if (err.contains("403")) add("The server cannot reach the stream provider right now." to CinematicaColors.Warn)
        }
    }
    if (lines.isEmpty()) return
    Column(
        Modifier
            .background(CinematicaColors.Surface, RoundedCornerShape(10.dp))
            .padding(horizontal = 12.dp, vertical = 8.dp),
    ) {
        lines.forEach { (text, colour) ->
            Text(text, style = MaterialTheme.typography.bodyMedium, color = colour)
        }
    }
}

/**
 * What the server is doing between OK and the first frame. Its `msg` is shown exactly as written —
 * "Candidate 2/5 too slow — trying the next…" says more than any progress bar can.
 */
@Composable
fun BufferingOverlay(job: io.github.superthom196.cinematica.browse.PlayJob) {
    Box(
        Modifier.fillMaxSize().background(Color(0xCC000000)),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            Modifier
                .width(620.dp)
                .background(CinematicaColors.Surface, RoundedCornerShape(16.dp))
                .padding(24.dp),
        ) {
            Text(job.title, style = MaterialTheme.typography.titleLarge, maxLines = 1, overflow = TextOverflow.Ellipsis)
            VSpace(14.dp)
            ProgressBar(job.pct, Modifier.fillMaxWidth(), height = 6.dp)
            VSpace(12.dp)
            Text(job.msg.orEmpty(), style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Text)
            VSpace(6.dp)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(job.label, style = MaterialTheme.typography.labelMedium, color = CinematicaColors.Accent)
                job.attemptLabel?.let {
                    HSpace(12.dp)
                    Text(it, style = MaterialTheme.typography.labelMedium, color = CinematicaColors.Muted)
                }
            }
            VSpace(10.dp)
            Text("Back to cancel.", style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted)
        }
    }
}
