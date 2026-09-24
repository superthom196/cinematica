package io.github.superthom196.cinematica.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
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
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import io.github.superthom196.cinematica.AppViewModel
import io.github.superthom196.cinematica.UiState
import io.github.superthom196.cinematica.api.ChannelInfo
import io.github.superthom196.cinematica.api.ChannelVideo
import io.github.superthom196.cinematica.api.Movie
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.launch

/**
 * A channel's detail page. Same shell as [SeriesDetailScreen] — banner, title, chips, overview —
 * minus the season pills a series has and this has no use for. Two panes: a video list on the
 * left, and on the right the chosen video's own page (thumbnail, facts, description, Play), which
 * is what OK on a row opens. Cinematica never plays a channel video itself — Play hands it to
 * whatever app on the box actually plays it, through [AppViewModel.playChannelVideo].
 */
@Composable
fun ChannelDetailScreen(vm: AppViewModel, ui: UiState, movie: Movie) {
    val id = movie.id
    val playJob by vm.play.job.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()

    var info by remember(id) { mutableStateOf<ChannelInfo?>(null) }
    var followed by remember(id) { mutableStateOf(movie.followed == true) }
    var subscribers by remember(id) { mutableStateOf(movie.subscribers) }
    var backdrop by remember(id) { mutableStateOf(movie.backdrop) }
    // The wall tile that opened this screen may be stale — stale enough that its own `followed`
    // is what decides whether NEW has already been cleared below, so it is read once, up front,
    // never from the state the refresh below is about to replace.
    val wasFollowed = remember(id) { movie.followed == true }

    LaunchedEffect(id) {
        if (id == null) return@LaunchedEffect
        if (wasFollowed) vm.channelSeen(id)
        vm.channel(id).getOrNull()?.let { c ->
            info = c
            followed = c.followed ?: followed
            subscribers = c.subscribers ?: subscribers
            backdrop = c.backdrop ?: backdrop
        }
    }

    var videos by remember(id) { mutableStateOf<List<ChannelVideo>>(emptyList()) }
    var nextPage by remember(id) { mutableStateOf<String?>(null) }
    var videosLoading by remember(id) { mutableStateOf(true) }
    var videosFailed by remember(id) { mutableStateOf(false) }
    var loadingMore by remember(id) { mutableStateOf(false) }

    suspend fun loadFirstPage() {
        if (id == null) return
        videosLoading = true
        videosFailed = false
        vm.channelVideos(id, null)
            .onSuccess { resp -> videos = resp.videos; nextPage = resp.next }
            .onFailure { videosFailed = true }
        videosLoading = false
    }
    LaunchedEffect(id) { loadFirstPage() }

    suspend fun loadMore() {
        val page = nextPage
        if (id == null || page == null || loadingMore) return
        loadingMore = true
        vm.channelVideos(id, page).onSuccess { resp ->
            val seen = videos.mapNotNull { it.id }.toSet()
            videos = videos + resp.videos.filter { it.id == null || it.id !in seen }
            nextPage = resp.next
        }
        loadingMore = false
    }

    var selected by remember(id) { mutableStateOf<ChannelVideo?>(null) }
    LaunchedEffect(videos) { if (selected == null) selected = videos.firstOrNull() }

    // Focus lands on the first video row once the list exists, as SeriesDetailScreen's episode
    // list does for its own first row.
    val firstRowFocus = remember { FocusRequester() }
    var focusedOnce by remember(id) { mutableStateOf(false) }
    LaunchedEffect(videos) {
        if (focusedOnce || videos.isEmpty()) return@LaunchedEffect
        focusedOnce = true
        runCatching { firstRowFocus.requestFocus() }
    }

    val playFocus = remember { FocusRequester() }
    LaunchedEffect(selected) {
        if (selected == null) return@LaunchedEffect
        withFrameNanos { }
        runCatching { playFocus.requestFocus() }
    }

    val listState = rememberLazyListState()
    val lastVisible by remember {
        derivedStateOf { listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0 }
    }
    LaunchedEffect(lastVisible, videos.size, nextPage) {
        if (nextPage != null && lastVisible >= videos.size - 5) loadMore()
    }

    // Back out of the buffering overlay stands the job down; Back anywhere else returns to the grid.
    BackHandler {
        if (playJob != null) vm.play.cancel() else vm.backFromDetail()
    }

    Box(Modifier.fillMaxSize()) {
        // A channel has no poster to fall back to — no banner means no picture at all, not the
        // avatar stretched over it.
        val hero = info?.backdrop ?: backdrop
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
                info?.title ?: movie.title.orEmpty(),
                style = MaterialTheme.typography.headlineMedium,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.fillMaxWidth(0.72f),
            )
            VSpace(6.dp)
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                channelChips(subscribers, followed).forEach { InfoChip(it) }
            }
            VSpace(6.dp)
            Text(
                info?.overview?.takeIf { it.isNotBlank() } ?: movie.overview.orEmpty(),
                style = MaterialTheme.typography.bodyMedium,
                color = CinematicaColors.Text,
                maxLines = 3, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.fillMaxWidth(0.72f),
            )
            VSpace(10.dp)
            PillButton(
                if (followed) "Following" else "Follow",
                onClick = {
                    if (id != null) {
                        val on = !followed
                        followed = on
                        vm.followChannel(id, on)
                    }
                },
                primary = followed,
            )
            VSpace(10.dp)
            Row(Modifier.fillMaxSize(), horizontalArrangement = Arrangement.spacedBy(24.dp)) {
                // ---- left: videos -------------------------------------------------------
                Column(Modifier.fillMaxWidth(0.42f).fillMaxHeight()) {
                    when {
                        videosLoading && videos.isEmpty() -> {
                            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                                Text("Finding videos…", style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Muted)
                            }
                        }
                        videosFailed && videos.isEmpty() -> {
                            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                                FocusSurface(onClick = { scope.launch { loadFirstPage() } }, container = CinematicaColors.Surface) {
                                    Text(
                                        "Couldn't load videos",
                                        style = MaterialTheme.typography.bodyMedium,
                                        color = CinematicaColors.Danger,
                                        modifier = Modifier.padding(horizontal = 14.dp, vertical = 8.dp),
                                    )
                                }
                            }
                        }
                        else -> {
                            LazyColumn(state = listState, modifier = Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                itemsIndexed(videos, key = { i, v -> v.id ?: "row-$i" }) { idx, v ->
                                    var rowFocused by remember(v.id) { mutableStateOf(false) }
                                    val on = v.id != null && selected?.id == v.id
                                    FocusSurface(
                                        onClick = { selected = v },
                                        modifier = Modifier.fillMaxWidth()
                                            .onFocusChanged { rowFocused = it.isFocused }
                                            .then(if (idx == 0) Modifier.focusRequester(firstRowFocus) else Modifier),
                                        container = if (on) CinematicaColors.SurfaceHigh else CinematicaColors.Surface,
                                        onLongClick = {
                                            val vid = v.id ?: return@FocusSurface
                                            val nowOpened = v.opened != true
                                            videos = videos.map { if (it.id == vid) it.copy(opened = nowOpened) else it }
                                            if (selected?.id == vid) selected = videos.firstOrNull { it.id == vid }
                                            if (id != null) vm.setVideoOpened(id, vid, nowOpened)
                                        },
                                    ) {
                                        Row(
                                            Modifier.fillMaxWidth().padding(horizontal = 10.dp, vertical = 6.dp)
                                                .alpha(if (v.opened == true && !rowFocused) 0.4f else 1f),
                                            verticalAlignment = Alignment.CenterVertically,
                                        ) {
                                            Box(Modifier.width(110.dp).aspectRatio(16f / 9f)) {
                                                PosterImage(v.thumb, Modifier.fillMaxSize(), corner = 4.dp)
                                                if (v.new == true) {
                                                    VideoNewBadge(Modifier.align(Alignment.TopStart).padding(2.dp))
                                                }
                                            }
                                            HSpace(10.dp)
                                            Column(Modifier.weight(1f)) {
                                                Text(
                                                    v.title.orEmpty(),
                                                    style = MaterialTheme.typography.bodyMedium.copy(
                                                        fontWeight = if (on) FontWeight.SemiBold else FontWeight.Normal,
                                                    ),
                                                    color = if (on) CinematicaColors.AccentBright else CinematicaColors.Text,
                                                    maxLines = 2, overflow = TextOverflow.Ellipsis,
                                                )
                                                videoMeta(v).takeIf { it.isNotEmpty() }?.let {
                                                    Text(it, style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted, maxLines = 1)
                                                }
                                            }
                                        }
                                    }
                                }
                                if (loadingMore) {
                                    item(key = "loading-more") {
                                        Box(Modifier.fillMaxWidth().padding(vertical = 8.dp), contentAlignment = Alignment.Center) {
                                            Text("Loading…", style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                // ---- right: the chosen video ------------------------------------------
                val v = selected
                if (v == null) {
                    Box(Modifier.weight(1f).fillMaxHeight(), contentAlignment = Alignment.Center) {
                        Text("Choose a video", style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Muted)
                    }
                } else {
                    VideoPane(
                        video = v,
                        playing = ui.playTitle != null,
                        playFocus = playFocus,
                        onPlay = {
                            val vid = v.id
                            if (id == null || vid == null) return@VideoPane
                            vm.playChannelVideo(id, vid) {
                                videos = videos.map { if (it.id == vid) it.copy(opened = true) else it }
                                selected = selected?.takeIf { it.id == vid }?.copy(opened = true) ?: selected
                            }
                        },
                        onStop = { vm.stopPlayback() },
                        modifier = Modifier.weight(1f).fillMaxHeight(),
                    )
                }
            }
        }
        playJob?.let { BufferingOverlay(it) }
    }
}

/** One video's page: thumbnail and facts up top, its description, Play. */
@Composable
private fun VideoPane(
    video: ChannelVideo,
    playing: Boolean,
    playFocus: FocusRequester,
    onPlay: () -> Unit,
    onStop: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier) {
        PosterImage(video.thumb, Modifier.fillMaxWidth(0.85f).aspectRatio(16f / 9f), corner = 8.dp)
        VSpace(10.dp)
        Text(
            video.title.orEmpty(),
            style = MaterialTheme.typography.titleLarge,
            maxLines = 2, overflow = TextOverflow.Ellipsis,
        )
        VSpace(6.dp)
        videoFacts(video).takeIf { it.isNotEmpty() }?.let {
            Text(it, style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted)
        }
        VSpace(8.dp)
        Text(
            video.description?.takeIf { it.isNotBlank() } ?: "No description.",
            style = MaterialTheme.typography.bodyMedium,
            color = if (video.description.isNullOrBlank()) CinematicaColors.Muted else CinematicaColors.Text,
            maxLines = 6, overflow = TextOverflow.Ellipsis,
        )
        VSpace(10.dp)
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
            PillButton("Play", onClick = onPlay, primary = true, modifier = Modifier.focusRequester(playFocus))
            // Only worth offering while this app actually has something open.
            if (playing) PillButton("Stop", onClick = onStop)
        }
    }
}

/** Same corner badge as a followed channel's NEW count on the wall, minus the count a single video has none of. */
@Composable
private fun VideoNewBadge(modifier: Modifier = Modifier) {
    Text(
        "NEW",
        style = MaterialTheme.typography.labelSmall.copy(fontSize = 10.sp, fontWeight = FontWeight.Bold),
        color = Color.White,
        modifier = modifier
            .background(CinematicaColors.Accent, RoundedCornerShape(4.dp))
            .padding(horizontal = 4.dp, vertical = 1.dp),
    )
}

/** Subscribers, formatted the way the wall's own numbers read, plus "Following" when it applies. */
private fun channelChips(subscribers: Long?, followed: Boolean): List<String> = buildList {
    subscribers?.let { add(formatSubscribers(it)) }
    if (followed) add("Following")
}

private fun formatSubscribers(n: Long): String {
    val count = when {
        n >= 1_000_000 -> "%.1fM".format(n / 1_000_000.0)
        n >= 100_000 -> "${Math.round(n / 1000.0)}K"
        else -> "%,d".format(n)
    }
    return "$count subscribers"
}

/** "3 days ago · 14 min" — the video row's own line. */
private fun videoMeta(v: ChannelVideo): String = buildList {
    v.published?.let { add(relativeAge(it)) }
    v.duration_s?.let { add(formatVideoDuration(it)) }
}.joinToString(" · ")

/** "3 days ago · 14 min · 1,204 views" — the chosen video's own line. */
private fun videoFacts(v: ChannelVideo): String = buildList {
    v.published?.let { add(relativeAge(it)) }
    v.duration_s?.let { add(formatVideoDuration(it)) }
    v.views?.let { add("%,d views".format(it)) }
}.joinToString(" · ")

private val dateFormatter by lazy { SimpleDateFormat("d MMM yyyy", Locale.getDefault()) }

/**
 * "today", "yesterday", up to 13 days as "N days ago", up to 8 weeks as "N weeks ago", then a date.
 * [published] is epoch SECONDS, as the server sends it.
 */
private fun relativeAge(published: Long): String {
    val publishedMs = published * 1000L
    val days = ((System.currentTimeMillis() - publishedMs) / 86_400_000L).toInt().coerceAtLeast(0)
    return when {
        days == 0 -> "today"
        days == 1 -> "yesterday"
        days <= 13 -> "$days days ago"
        days <= 62 -> "${days / 7} weeks ago"
        else -> dateFormatter.format(Date(publishedMs))
    }
}

/** "14 min" under an hour, "1 h 5 min" over it. */
private fun formatVideoDuration(seconds: Int): String {
    val h = seconds / 3600
    val m = (seconds % 3600) / 60
    return if (h > 0) "$h h $m min" else "$m min"
}
