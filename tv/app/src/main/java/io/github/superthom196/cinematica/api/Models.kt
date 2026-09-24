package io.github.superthom196.cinematica.api

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * The server omits keys freely (a field that has not been computed yet, an error path that skips
 * the happy-path fields, …), so every property here is nullable with a default: a partial JSON
 * object always decodes instead of throwing.
 */
val json = Json {
    ignoreUnknownKeys = true
    isLenient = true
    coerceInputValues = true
    explicitNulls = false
}

@Serializable
data class Pick(
    val infoHash: String? = null,
    val fileIdx: Int? = null,
    val seeders: Int? = null,
    val gb: Double? = null,
    val provider: String? = null,
    val tag: String? = null,
    val codec: String? = null,
    val pack: String? = null,
    val display: String? = null,
    val is4k: Boolean? = null,
    val req_mbps: Double? = null,
    val budget_mbps: Double? = null,
    val thin: Boolean? = null,
    val fallback: Boolean? = null,
    val audio: String? = null,
    val score: Double? = null,
    val audio_actual: String? = null,
    val transcoded: Boolean? = null,
)

@Serializable
data class StreamInfo(
    val pick: Pick? = null,
    val count: Int? = null,
    val url: String? = null,
    val err: String? = null,
)

@Serializable
data class Imdb(
    val rating: Double? = null,
    val votes: Int? = null,
    val id: String? = null,
)

/**
 * What the server remembers about a title: saved, watched, where the viewer got to. Absent on
 * a server that predates the shelf, and absent on a title nothing is known about — which is the
 * same thing to every screen here, so nothing ever has to tell the two apart.
 */
@Serializable
data class Shelf(
    val fav: Boolean = false,
    val watched: Boolean = false,
    val pinned: Boolean = false,
    /** 0..1, or null. A film's own position; for a series, its in-progress episode's. */
    val progress: Float? = null,
    /** Seconds to start from, already backed off a few seconds by the server. Films only. */
    val resume_s: Int? = null,
    val next: ShelfNext? = null,
)

/** Where a series carries on. A null [resume_s] means the start of that episode. */
@Serializable
data class ShelfNext(val s: Int, val e: Int, val resume_s: Int? = null)

@Serializable
data class Movie(
    val id: String? = null,
    val title: String? = null,
    /**
     * A string on the wire, not a number: the server slices it out of the provider's `release_date`
     * (`(r.get("release_date") or "")[:4]`), so an unreleased film yields `""` — which would fail
     * to decode as an Int and take the whole page down with it.
     */
    val year: String? = null,
    val poster: String? = null,
    val backdrop: String? = null,
    val vote: Double? = null,
    val votes: Int? = null,
    val genre_ids: List<Int>? = null,
    val overview: String? = null,
    val stream: StreamInfo? = null,
    val imdb: Imdb? = null,
    val boost: Double? = null,
    val kind: String? = null,
    val shelf: Shelf? = null,
    /** Channel wall items only: subscriber count, last upload time, follow state, unseen-upload count. */
    val subscribers: Long? = null,
    val latest_at: Long? = null,
    val followed: Boolean? = null,
    val new: Int? = null,
)

@Serializable
data class MoviesPage(
    val movies: List<Movie>? = null,
    /**
     * At offset 0 only: the titles the viewer is part-way through, most recently touched first.
     * They go in front of the catalogue, and a later page repeating one of them drops it. Built
     * from a stored snapshot, so they may carry no stream, rating or quality at all.
     */
    val pinned: List<Movie> = emptyList(),
    val offset: Int? = null,
    val limit: Int? = null,
    val more: Boolean? = null,
    val sort: String? = null,
    val checked: Int? = null,
    val pool: Int? = null,
    /** Channel pool only: channels not yet followed, offered below the followed ones. */
    val popular: List<Movie> = emptyList(),
    val channel_ops: List<String> = emptyList(),
    val err: String? = null,
)

/** `/api/movies/progress`: what a cold view is doing, for the ring over an empty grid. */
@Serializable
data class ViewProgress(
    val stage: String? = null,
    val fraction: Float? = null,
    val label: String? = null,
    val elapsed: Float? = null,
)

@Serializable
data class Genre(
    val id: String? = null,
    val name: String? = null,
)

@Serializable
data class GenresResp(
    val genres: List<Genre>? = null,
)

@Serializable
data class MovieDetail(
    val id: String? = null,
    val title: String? = null,
    val tagline: String? = null,
    val overview: String? = null,
    val runtime: Int? = null,
    val vote: Double? = null,
    val votes: Int? = null,
    val release: String? = null,
    // A provider may know the year without knowing the exact release date,
    // so this is not always derivable from `release`.
    val year: String? = null,
    val genres: List<String>? = null,
    val backdrop: String? = null,
    val poster: String? = null,
    val imdb_id: String? = null,
    val shelf: Shelf? = null,
)

@Serializable
data class Season(
    val n: Int? = null,
    val name: String? = null,
    val episodes: Int? = null,
    val air: String? = null,
    val poster: String? = null,
)

@Serializable
data class TvDetail(
    val id: String? = null,
    val kind: String? = null,
    val title: String? = null,
    val overview: String? = null,
    val tagline: String? = null,
    val runtime: Int? = null,
    val vote: Double? = null,
    val votes: Int? = null,
    @SerialName("first_air") val firstAir: String? = null,
    @SerialName("last_air") val lastAir: String? = null,
    val status: String? = null,
    val genres: List<String>? = null,
    val seasons: List<Season>? = null,
    val backdrop: String? = null,
    val poster: String? = null,
    @SerialName("imdb_id") val imdbId: String? = null,
    val shelf: Shelf? = null,
)

@Serializable
data class Episode(
    val season: Int? = null,
    val episode: Int? = null,
    val name: String? = null,
    val overview: String? = null,
    val runtime: Int? = null,
    val air: String? = null,
    val still: String? = null,
    val vote: Double? = null,
    // An episode has no shelf object of its own: the three things worth knowing about one are
    // flat on the episode, and a series' fav/pinned belong to the series.
    val watched: Boolean = false,
    val progress: Float? = null,
    val resume_s: Int? = null,
)

@Serializable
data class SeasonResp(
    val episodes: List<Episode>? = null,
)

@Serializable
data class PlayResp(
    val ok: Boolean? = null,
    val msg: String? = null,
    val url: String? = null,
    val pick: Pick? = null,
    val job: String? = null,
)

@Serializable
data class Progress(
    val stage: String? = null,
    val pct: Double? = null,
    val msg: String? = null,
    val got: Long? = null,
    val target: Long? = null,
    val speed: Double? = null,
    val ok: Boolean? = null,
    val url: String? = null,
    val title: String? = null,
    val attempt: Int? = null,
    val attempts: Int? = null,
    val candidate: String? = null,
    val req_mbps: Double? = null,
)

@Serializable
data class NowPlaying(
    val playing: Boolean? = null,
    val paused: Boolean? = null,
    val live: Boolean? = null,
    val state: Int? = null,
    val title: String? = null,
    val tag: String? = null,
    val audio: String? = null,
    val transcoded: Boolean? = null,
    val gb: Double? = null,
    val source: String? = null,
    val position_s: Double? = null,
    val duration_s: Double? = null,
)

@Serializable
data class AppInfo(
    val id: String? = null,
    val name: String? = null,
    val version: String? = null,
    val state: String? = null,
    val seen_s: Double? = null,
)

@Serializable
data class AdbInfo(
    val enabled: Boolean? = null,
    val state: String? = null,
)

@Serializable
data class Health(
    val tv: Boolean? = null,
    val tv_msg: String? = null,
    val tv_state: String? = null,
    val movies: Int? = null,
    val imdb_titles: Int? = null,
    val page_size: Int? = null,
    val streams_cached: Int? = null,
    val providers: ProvidersHealth? = null,
    val fourk_only: Boolean? = null,
    val hevc_only: Boolean? = null,
    val min_seeders: Int? = null,
    val max_gb: Double? = null,
    val sustain_mbps: Double? = null,
    val conns: Int? = null,
    val net_source: String? = null,
    val app: AppInfo? = null,
    val adb: AdbInfo? = null,
)

/**
 * `providers_health()`: whether the server has anything configured to serve a catalogue and a
 * stream source. `configured` false is a SETUP state, not a failure — a fresh install answers
 * `/api/health` with this same shape, just nothing chosen yet.
 */
@Serializable
data class ProvidersHealth(
    val configured: Boolean? = null,
    val message: String? = null,
    val roles: Map<String, RoleHealth> = emptyMap(),
)

/** One role's provider assignment, keyed by role name ("catalogue", "channels", …) in [ProvidersHealth.roles]. */
@Serializable
data class RoleHealth(
    val provider: String? = null,
    val ready: Boolean? = null,
)

@Serializable
data class NetSample(
    val mbps: Double? = null,
    val seeders: Int? = null,
    val at: Double? = null,
)

@Serializable
data class NetCheck(
    val at: Double? = null,
    val mbps: Double? = null,
    val conns: Int? = null,
    val http_mbps: Double? = null,
    val source: String? = null,
    val n_samples: Int? = null,
    val n_ordinary: Int? = null,
    val samples: List<NetSample>? = null,
    val sustain_live: Double? = null,
    val busy: Boolean? = null,
    val first_run: Boolean? = null,
    val needs: Int? = null,
    val well_seeded_at: Double? = null,
    val calibrating: Boolean? = null,
    val cal_done: Int? = null,
    val cal_want: Int? = null,
    val cal_msg: String? = null,
    val age_h: Double? = null,
    val stale_read: Boolean? = null,
)

@Serializable
data class OkResp(
    val ok: Boolean? = null,
    val msg: String? = null,
    val state: String? = null,
    val job: String? = null,
)

@Serializable
data class AppCmd(
    val seq: Long? = null,
    val type: String? = null,
    val job: String? = null,
    val url: String? = null,
    val title: String? = null,
    val pick: Pick? = null,
    val transcoded: Boolean? = null,
    val hifi: Boolean = false,
    /** Where to start, when the play was a resume. Absent on an ordinary play and on autoplay-next. */
    val start_s: Double? = null,
)

@Serializable
data class HeartbeatResp(
    val ok: Boolean? = null,
    val cmd: AppCmd? = null,
    val sync: SyncInfo? = null,
    val hifi_status: HifiStatus? = null,
)

/**
 * Where the hifi audio for the film on screen has got to, on every heartbeat while one is up:
 * `live` (a `sync` verdict rides alongside), `starting` (the decoder is seeking, or the bridge is
 * being connected), `stopped` (paused), or `failed` with [msg] saying why — shown on the OSD so a
 * film with no sound never looks like a film with the volume down.
 */
@Serializable
data class HifiStatus(
    val state: String,
    val msg: String? = null,
)

/**
 * The server's lip-sync verdict for the current hifi film, from the position the TV last reported.
 * `err_s` = tv_position_s − audio_pos_s: positive means the picture is ahead of the audio the
 * server is streaming out of band, so the TV must slow down (or seek back) to let audio catch up.
 */
@Serializable
data class SyncInfo(
    val gen: Int,
    val audio_pos_s: Double,
    val err_s: Double,
)

@Serializable
data class HeartbeatBody(
    val id: String,
    val name: String,
    val version: String,
    val state: String,
    val title: String? = null,
    val job: String? = null,
    val position_s: Double? = null,
    val duration_s: Double? = null,
    val ack: Long? = null,
    val err: String? = null,
    val wait: Double? = null,
    val hifi: Boolean = false,
    val hifi_player: String? = null,
    val hifi_delay_ms: Int? = null,
    val seek_seq: Long? = null,
)

@Serializable
data class HifiPlayer(
    val id: String,
    val name: String,
    val url: String,
    val connected: Boolean = false,
)

@Serializable
data class HifiPlayersResp(
    val ok: Boolean = false,
    val players: List<HifiPlayer> = emptyList(),
    val default: String? = null,
)

@Serializable
data class SearchDone(
    val playable: Int? = null,
    val found: Int? = null,
    val checked: Int? = null,
)

@Serializable
data class SearchFound(
    val found: Int? = null,
)

/** `/api/shelf`: the watchlist, newest save first, films and series mixed, less what was watched since. */
@Serializable
data class ShelfPage(
    val items: List<Movie> = emptyList(),
)

/** What the fav and watched posts answer with: the title's shelf as the server now has it. */
@Serializable
data class ShelfResp(
    val ok: Boolean? = null,
    val shelf: Shelf? = null,
)

/**
 * Enough of a title to draw it on the watchlist after it has fallen out of the catalogue.
 * Sent with every save, since the server has nowhere else to get it from.
 */
@Serializable
data class ShelfSnap(
    val kind: String? = null,
    val title: String? = null,
    val year: String? = null,
    val poster: String? = null,
    val imdb_id: String? = null,
)

@Serializable
data class FavReq(val id: String, val on: Boolean, val snap: ShelfSnap? = null)

/** `s` and `e` together mean one episode; absent, the whole title. */
@Serializable
data class WatchedReq(val id: String, val on: Boolean, val s: Int? = null, val e: Int? = null)

@Serializable
data class DropReq(val job: String)

// ---- channels -----------------------------------------------------------

/** One upload on `/api/channel/videos`. */
@Serializable
data class ChannelVideo(
    val id: String? = null,
    val title: String? = null,
    /** Epoch seconds. */
    val published: Long? = null,
    val duration_s: Int? = null,
    val thumb: String? = null,
    val description: String? = null,
    val views: Long? = null,
    val opened: Boolean? = null,
    val new: Boolean? = null,
)

/** `next == ""` means "there is more, ask again with an empty page token"; `null` means no more. */
@Serializable
data class ChannelVideosResp(
    val videos: List<ChannelVideo> = emptyList(),
    val next: String? = null,
)

/** `/api/channel`: one wall item plus the ops this server's channel provider supports. */
@Serializable
data class ChannelInfo(
    val id: String? = null,
    val kind: String? = null,
    val title: String? = null,
    val poster: String? = null,
    val backdrop: String? = null,
    val overview: String? = null,
    val subscribers: Long? = null,
    val latest_at: Long? = null,
    val followed: Boolean? = null,
    val new: Int? = null,
    val channel_ops: List<String> = emptyList(),
)

/** What `/api/channel/play` hands back: the link, which app should open it, and its name for a toast. */
@Serializable
data class ChannelPlay(
    val url: String,
    @SerialName("package") val pkg: String = "",
    val label: String = "another app",
)

/** 502 (the external app's link could not be resolved) decodes the same shape, with `ok=false` and [msg]. */
@Serializable
data class ChannelPlayResp(
    val ok: Boolean = false,
    val play: ChannelPlay? = null,
    val msg: String? = null,
)

/** `/api/channel/follow`: the channel's own wall item as the server now has it. */
@Serializable
data class ChannelFollowResp(
    val ok: Boolean? = null,
    val channel: Movie? = null,
)

@Serializable
data class ChannelIdReq(val id: String)

@Serializable
data class ChannelFollowReq(val id: String, val on: Boolean)

@Serializable
data class ChannelOpenedReq(val id: String, val video: String, val on: Boolean)

@Serializable
data class ChannelPlayReq(val id: String, val video: String)
