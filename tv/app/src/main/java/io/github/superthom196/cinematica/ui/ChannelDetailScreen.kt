package io.github.superthom196.cinematica.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.shape.CircleShape
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
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
 * A channel's page: one slim row for the channel itself, and the rest of the screen for its videos,
 * four across, newest first -- the titles are what this screen is for, so nothing else takes
 * their room. OK on a video hands it to whatever app on the box plays it, through
 * [AppViewModel.playChannelVideo]; Cinematica never plays one itself. Long-press marks it watched
 * or not. Older uploads page in as the grid nears its end, when the server says there are more.
 */
@Composable
fun ChannelDetailScreen(vm: AppViewModel, ui: UiState, movie: Movie) {
    val id = movie.id
    val playJob by vm.play.job.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()

    var info by remember(id) { mutableStateOf<ChannelInfo?>(null) }
    var followed by remember(id) { mutableStateOf(movie.followed == true) }
    var subscribers by remember(id) { mutableStateOf(movie.subscribers) }
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

    // Focus lands on the newest video once the grid exists; Up from the top row reaches Follow.
    val firstTileFocus = remember { FocusRequester() }
    var focusedOnce by remember(id) { mutableStateOf(false) }
    LaunchedEffect(videos) {
        if (focusedOnce || videos.isEmpty()) return@LaunchedEffect
        focusedOnce = true
        withFrameNanos { }
        runCatching { firstTileFocus.requestFocus() }
    }

    val gridState = rememberLazyGridState()
    val lastVisible by remember {
        derivedStateOf { gridState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0 }
    }
    // Two rows ahead of the end, so the next page is usually in before the viewer gets there.
    LaunchedEffect(lastVisible, videos.size, nextPage) {
        if (nextPage != null && lastVisible >= videos.size - VIDEO_COLUMNS * 2) loadMore()
    }

    // Back out of the buffering overlay stands the job down; Back anywhere else returns to the grid.
    BackHandler {
        if (playJob != null) vm.play.cancel() else vm.backFromDetail()
    }

    Box(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize().padding(start = 40.dp, end = 40.dp, top = 24.dp)) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(48.dp).clip(CircleShape)) {
                    PosterImage(info?.poster ?: movie.poster, Modifier.fillMaxSize(), corner = 0.dp)
                }
                HSpace(14.dp)
                Column(Modifier.weight(1f)) {
                    Text(
                        info?.title ?: movie.title.orEmpty(),
                        style = MaterialTheme.typography.headlineSmall,
                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                    )
                    subscribers?.let {
                        Text(formatSubscribers(it), style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted)
                    }
                }
                HSpace(14.dp)
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
            }
            VSpace(12.dp)
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
                videos.isEmpty() -> {
                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        Text("No videos yet", style = MaterialTheme.typography.bodyMedium, color = CinematicaColors.Muted)
                    }
                }
                else -> {
                    LazyVerticalGrid(
                        state = gridState,
                        columns = GridCells.Fixed(VIDEO_COLUMNS),
                        modifier = Modifier.fillMaxSize(),
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                        verticalArrangement = Arrangement.spacedBy(12.dp),
                        // Room for the focused tile's scale-up at the edges, and to scroll clear of the bottom.
                        contentPadding = PaddingValues(start = 8.dp, end = 8.dp, top = 8.dp, bottom = 40.dp),
                    ) {
                        itemsIndexed(videos, key = { i, v -> v.id ?: "row-$i" }) { idx, v ->
                            VideoTile(
                                video = v,
                                modifier = if (idx == 0) Modifier.focusRequester(firstTileFocus) else Modifier,
                                onPlay = {
                                    val vid = v.id
                                    if (id != null && vid != null) {
                                        vm.playChannelVideo(id, vid) {
                                            videos = videos.map { if (it.id == vid) it.copy(opened = true) else it }
                                        }
                                    }
                                },
                                onToggleOpened = {
                                    val vid = v.id
                                    if (vid != null) {
                                        val nowOpened = v.opened != true
                                        videos = videos.map { if (it.id == vid) it.copy(opened = nowOpened) else it }
                                        if (id != null) vm.setVideoOpened(id, vid, nowOpened)
                                    }
                                },
                            )
                        }
                        if (loadingMore) {
                            item(key = "loading-more", span = { GridItemSpan(maxLineSpan) }) {
                                Box(Modifier.fillMaxWidth().padding(vertical = 8.dp), contentAlignment = Alignment.Center) {
                                    Text("Loading older videos…", style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted)
                                }
                            }
                        }
                    }
                }
            }
        }
        playJob?.let { BufferingOverlay(it) }
    }
}

private const val VIDEO_COLUMNS = 4

/** One video: thumbnail, its title in full as far as three lines allow, then age and length. */
@Composable
private fun VideoTile(
    video: ChannelVideo,
    onPlay: () -> Unit,
    onToggleOpened: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var focused by remember(video.id) { mutableStateOf(false) }
    FocusSurface(
        onClick = onPlay,
        onLongClick = onToggleOpened,
        modifier = modifier.fillMaxWidth().onFocusChanged { focused = it.isFocused },
        shape = RoundedCornerShape(8.dp),
        container = Color.Transparent,
    ) {
        // Watched ones step back, but not while focused: the one being looked at is always legible.
        Column(Modifier.padding(6.dp).alpha(if (video.opened == true && !focused) 0.4f else 1f)) {
            Box(Modifier.fillMaxWidth().aspectRatio(16f / 9f)) {
                PosterImage(video.thumb, Modifier.fillMaxSize(), corner = 6.dp)
                if (video.new == true) VideoNewBadge(Modifier.align(Alignment.TopStart).padding(4.dp))
                video.duration_s?.let {
                    Text(
                        formatVideoDuration(it),
                        style = MaterialTheme.typography.labelSmall,
                        color = Color.White,
                        modifier = Modifier.align(Alignment.BottomEnd).padding(4.dp)
                            .background(Color(0xCC000000), RoundedCornerShape(4.dp))
                            .padding(horizontal = 4.dp, vertical = 1.dp),
                    )
                }
            }
            VSpace(6.dp)
            Text(
                video.title.orEmpty(),
                style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold),
                color = if (focused) CinematicaColors.AccentBright else CinematicaColors.Text,
                maxLines = 3, overflow = TextOverflow.Ellipsis,
                minLines = 3,
            )
            video.published?.let {
                Text(relativeAge(it), style = MaterialTheme.typography.bodySmall, color = CinematicaColors.Muted, maxLines = 1)
            }
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

private fun formatSubscribers(n: Long): String {
    val count = when {
        n >= 1_000_000 -> "%.1fM".format(n / 1_000_000.0)
        n >= 100_000 -> "${Math.round(n / 1000.0)}K"
        else -> "%,d".format(n)
    }
    return "$count subscribers"
}

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
