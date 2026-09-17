package io.github.superthom196.cinematica.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.tv.material3.MaterialTheme
import androidx.tv.material3.Text
import io.github.superthom196.cinematica.api.Movie
import io.github.superthom196.cinematica.api.Pick

/**
 * One film on the poster wall.
 *
 * Everything on it comes from the grid's own data — the list is already filtered to films with a
 * playable stream, so the badge and the bitrate warning need no extra call. [progress] paints this
 * film's own play job on the tile while one is running.
 */
@Composable
fun MovieTile(
    movie: Movie,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    progress: Double? = null,
) {
    val pick: Pick? = movie.stream?.pick
    val isTv = movie.kind == "tv"
    GridTile(onClick = onClick, modifier = modifier) { _ ->
        Column(Modifier.padding(4.dp)) {
            Box(Modifier.fillMaxWidth().aspectRatio(2f / 3f)) {
                PosterImage(movie.poster, Modifier.fillMaxWidth().aspectRatio(2f / 3f), corner = 6.dp)
                if (pick != null || isTv) {
                    Row(
                        Modifier.align(Alignment.TopStart).padding(3.dp),
                        horizontalArrangement = Arrangement.spacedBy(3.dp),
                    ) {
                        // A series gets its own badge; the plain quality tag (HD/whatever the
                        // server sent) is dropped alongside it — "TV" already says enough unless
                        // the pick is genuinely 4K, which is worth calling out either way.
                        if (isTv) TvBadge()
                        if (pick != null && (pick.is4k == true || !isTv)) QualityBadge(pick)
                    }
                }
                val imdb = movie.imdb
                if (imdb?.rating != null) {
                    RatingBadge(imdb.rating, movie.boost, Modifier.align(Alignment.BottomEnd).padding(3.dp))
                }
            }
            VSpace(4.dp)
            Text(
                movie.title.orEmpty(),
                style = MaterialTheme.typography.titleSmall.copy(fontSize = 12.sp, lineHeight = 15.sp),
                maxLines = 2, minLines = 2, overflow = TextOverflow.Ellipsis,
            )
            Text(
                movie.year.orEmpty(),
                style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.sp, lineHeight = 14.sp),
                color = CinematicaColors.Muted, maxLines = 1,
            )
            // The picker has already chosen the best available release, so there is nothing to
            // choose between here. The one thing the tag cannot say is that this pick is a heavy
            // re-encode that will not look like its badge — so say that, and nothing else.
            when {
                pick == null -> Text(
                    "no stream",
                    style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.sp, lineHeight = 14.sp, fontWeight = FontWeight.SemiBold),
                    color = CinematicaColors.Danger, maxLines = 1,
                )
                pick.thin == true -> Text(
                    "low bitrate",
                    style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.sp, lineHeight = 14.sp, fontWeight = FontWeight.SemiBold),
                    color = CinematicaColors.Warn, maxLines = 1,
                )
                else -> Text("", style = MaterialTheme.typography.bodySmall.copy(fontSize = 11.sp, lineHeight = 14.sp), maxLines = 1)
            }
            if (progress != null) {
                VSpace(3.dp)
                ProgressBar(progress, Modifier.fillMaxWidth(), height = 3.dp)
            }
        }
    }
}

/** Marks a series tile. Sits before the quality badge when both apply. */
@Composable
private fun TvBadge(modifier: Modifier = Modifier) {
    Text(
        "TV",
        style = MaterialTheme.typography.labelSmall.copy(fontSize = 10.sp, fontWeight = FontWeight.Bold),
        color = Color.White,
        modifier = modifier
            .background(Color(0xCC000000), RoundedCornerShape(4.dp))
            .padding(horizontal = 4.dp, vertical = 1.dp),
    )
}

/** `4K` in the accent when the pick really is 4K, otherwise the server's own tag. */
@Composable
private fun QualityBadge(pick: Pick, modifier: Modifier = Modifier) {
    val is4k = pick.is4k == true
    Text(
        if (is4k) "4K" else (pick.tag ?: "HD"),
        style = MaterialTheme.typography.labelSmall.copy(fontSize = 10.sp, fontWeight = FontWeight.Bold),
        color = Color.White,
        modifier = modifier
            .background(if (is4k) CinematicaColors.Accent else Color(0xCC000000), RoundedCornerShape(4.dp))
            .padding(horizontal = 4.dp, vertical = 1.dp),
    )
}

/** The IMDb rating, plus the recency bonus the `balanced` sort added, if any. */
@Composable
private fun RatingBadge(rating: Double, boost: Double?, modifier: Modifier = Modifier) {
    Row(
        modifier
            .background(Color(0xCC000000), RoundedCornerShape(4.dp))
            .padding(horizontal = 4.dp, vertical = 1.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            "%.1f".format(rating),
            style = MaterialTheme.typography.labelSmall.copy(fontSize = 10.sp, fontWeight = FontWeight.Bold),
            color = Color.White,
        )
        if (boost != null && boost > 0.0) {
            Text(
                " +%.1f".format(boost),
                style = MaterialTheme.typography.labelSmall.copy(fontSize = 10.sp, fontWeight = FontWeight.SemiBold),
                color = CinematicaColors.Boost,
            )
        }
    }
}
