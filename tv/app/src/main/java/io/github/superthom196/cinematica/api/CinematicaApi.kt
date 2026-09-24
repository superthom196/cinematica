package io.github.superthom196.cinematica.api

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

private const val DEFAULT_PORT = 8090

/**
 * A provider-qualified title id ("cinemeta:tt0903747") carries a colon, which is not safe to drop
 * straight into a URL path segment — so every id this class interpolates into a path goes through
 * this first. `URLEncoder` is form encoding (space -> "+"), which is wrong for a path segment, so
 * that one substitution is undone.
 */
internal fun encodePathSegment(id: String): String =
    java.net.URLEncoder.encode(id, "UTF-8").replace("+", "%20")

/**
 * The `/api/movies` query string. [genres] and [exclude] are only appended when non-empty,
 * `kind=tv` only when [kind] is "tv", and `bias` is always said explicitly so the grid never
 * depends on the server's default.
 */
internal fun buildMoviesQuery(
    offset: Int,
    limit: Int,
    sort: String,
    genres: Set<String>,
    exclude: Set<String>,
    kind: String = "movie",
    bias: Boolean = true,
): String = buildString {
    append("/api/movies?offset=").append(offset)
    append("&limit=").append(limit)
    append("&sort=").append(sort)
    if (genres.isNotEmpty()) append("&genres=").append(genres.joinToString(","))
    if (exclude.isNotEmpty()) append("&exclude=").append(exclude.joinToString(","))
    if (kind == "tv") append("&kind=tv")
    append("&bias=").append(if (bias) 1 else 0)
}

/** Same view parameters as [buildMoviesQuery]: the `/api/movies/progress` query string. */
internal fun buildProgressQuery(sort: String, genres: Set<String>, exclude: Set<String>, kind: String, bias: Boolean): String =
    buildString {
        append("/api/movies/progress?sort=").append(sort)
        if (genres.isNotEmpty()) append("&genres=").append(genres.joinToString(","))
        if (exclude.isNotEmpty()) append("&exclude=").append(exclude.joinToString(","))
        if (kind == "tv") append("&kind=tv")
        append("&bias=").append(if (bias) 1 else 0)
    }

/** Normalises whatever the user typed ("cinematica.lan:8090", "192.168.1.50", a pasted URL) into "http://host[:port]". */
fun normaliseBaseUrl(raw: String): String {
    var host = raw.trim()
    host = host.removePrefix("http://").removePrefix("https://").trimEnd('/')
    if (host.isBlank()) host = "localhost"
    return if (host.contains(":")) "http://$host" else "http://$host:$DEFAULT_PORT"
}

/**
 * A short, human reason for a failed request: "no such host", not
 * "java.net.UnknownHostException: Unable to resolve host …".
 */
fun Throwable.friendly(): String = when (this) {
    is java.net.UnknownHostException -> "no such host"
    is java.net.ConnectException -> "connection refused"
    is java.net.SocketTimeoutException -> "timed out"
    is java.net.NoRouteToHostException -> "no route to the server"
    is javax.net.ssl.SSLException -> "TLS error"
    is kotlinx.serialization.SerializationException -> "unreadable answer from the server"
    is IOException -> message?.takeIf { it.isNotBlank() } ?: "network error"
    else -> message?.takeIf { it.isNotBlank() } ?: (this::class.simpleName ?: "unknown error")
}

/**
 * Talks to the Cinematica server. [baseUrlProvider] is read on every call so a host change in
 * settings takes effect on the next request without recreating this object.
 */
class CinematicaApi(private val baseUrlProvider: () -> String) {

    private val jsonMedia = "application/json; charset=utf-8".toMediaType()

    // /api/movies, /api/search and /api/search/stream can take a while: a cold search resolves
    // dozens of stream candidates. Everything else should fail fast.
    // `/api/movies` on a cold view assembles the whole pool first — a biased film pool is ~60 catalogue
    // pages, minutes rather than seconds — and a timeout there paints "Failed" over a server that
    // is simply working. The grid shows the server's own progress ring meanwhile.
    private val longReadClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(300, TimeUnit.SECONDS)
        .build()

    private val shortReadClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    // The heartbeat can long-poll on the server for up to 10s (the "wait" it sends, capped there),
    // so its read timeout needs real headroom beyond that without being as generous as the search
    // clients. Its own client, too: a heartbeat must never queue behind a slow /api/movies.
    private val heartbeatClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    private val base: String get() = baseUrlProvider()

    /**
     * Sends the request and reads the whole body off the main thread.
     *
     * OkHttp's `enqueue` runs the call on its own dispatcher, but the continuation resumes on
     * whatever dispatcher the caller was on — the main thread, for anything started from
     * viewModelScope — and `body.string()` is a blocking socket read. That is a
     * NetworkOnMainThreadException every time, which is why this whole function is confined to IO.
     *
     * [allowNonSuccess] is for /api/play only: it documents 202/409/502 responses with a JSON body
     * the caller decodes, so those statuses must not become an exception. Everything else throws on
     * a non-2xx, or a 500 with a JSON body silently decodes as success.
     */
    private suspend fun body(client: OkHttpClient, request: Request, allowNonSuccess: Boolean = false): String =
        withContext(Dispatchers.IO) {
            execute(client, request).use { response ->
                val text = response.body.string().ifBlank { "{}" }
                if (!allowNonSuccess && !response.isSuccessful) {
                    throw IOException("HTTP ${response.code}: ${text.take(120)}")
                }
                text
            }
        }

    private suspend fun execute(client: OkHttpClient, request: Request): Response =
        suspendCancellableCoroutine { cont ->
            val call = client.newCall(request)
            cont.invokeOnCancellation { call.cancel() }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    if (!cont.isCancelled) cont.resumeWithException(e)
                }
                override fun onResponse(call: Call, response: Response) {
                    cont.resume(response)
                }
            })
        }

    private suspend inline fun <reified T> get(client: OkHttpClient, path: String, allowNonSuccess: Boolean = false): T {
        val request = Request.Builder().url("$base$path").get().build()
        val text = body(client, request, allowNonSuccess)
        return withContext(Dispatchers.Default) { json.decodeFromString<T>(text) }
    }

    private suspend inline fun <reified T> post(
        client: OkHttpClient,
        path: String,
        bodyJson: String? = null,
        allowNonSuccess: Boolean = false,
    ): T {
        val request = Request.Builder().url("$base$path")
            .post((bodyJson ?: "{}").toRequestBody(jsonMedia))
            .build()
        val text = body(client, request, allowNonSuccess)
        return withContext(Dispatchers.Default) { json.decodeFromString<T>(text) }
    }

    suspend fun genres(kind: String = "movie"): GenresResp =
        get(shortReadClient, "/api/genres" + if (kind == "tv") "?kind=tv" else "")

    suspend fun movies(
        offset: Int,
        limit: Int,
        sort: String,
        genres: Set<String>,
        exclude: Set<String>,
        kind: String = "movie",
        bias: Boolean = true,
    ): MoviesPage = get(longReadClient, buildMoviesQuery(offset, limit, sort, genres, exclude, kind, bias))

    /** Same view parameters as [movies]: what that request is doing right now. Cheap. */
    suspend fun progress(sort: String, genres: Set<String>, exclude: Set<String>, kind: String, bias: Boolean): ViewProgress =
        get(shortReadClient, buildProgressQuery(sort, genres, exclude, kind, bias))

    suspend fun movie(id: String): MovieDetail = get(shortReadClient, "/api/movie/${encodePathSegment(id)}")

    suspend fun tv(id: String): TvDetail = get(shortReadClient, "/api/tv/${encodePathSegment(id)}")

    suspend fun tvSeason(id: String, n: Int): SeasonResp =
        get(shortReadClient, "/api/tv/${encodePathSegment(id)}/season/$n")

    suspend fun stream(id: String, force: Boolean = false): StreamInfo =
        get(shortReadClient, "/api/stream/${encodePathSegment(id)}" + if (force) "?force=1" else "")

    suspend fun streamTv(id: String, s: Int, e: Int, force: Boolean = false): StreamInfo =
        get(shortReadClient, "/api/stream/tv/${encodePathSegment(id)}/$s/$e" + if (force) "?force=1" else "")

    /**
     * 202 (buffering), 409 (already active / no stream) and 502 (TV unreachable) all decode as PlayResp.
     * [t] is where to start, in seconds, for a resume; left off, the film starts at the beginning.
     */
    suspend fun play(id: String, t: Int? = null): PlayResp =
        post(
            shortReadClient,
            "/api/play/${encodePathSegment(id)}" + if (t != null) "?t=$t" else "",
            allowNonSuccess = true,
        )

    /** Same 202/409/502-as-PlayResp contract as [play]; same [t]. */
    suspend fun playTv(id: String, s: Int, e: Int, autoplay: Boolean, t: Int? = null): PlayResp =
        post(
            shortReadClient,
            "/api/play/tv/${encodePathSegment(id)}/$s/$e?autoplay=" + (if (autoplay) "1" else "0") +
                (if (t != null) "&t=$t" else ""),
            allowNonSuccess = true,
        )

    /** An empty `{}` body (no job yet) decodes to a Progress with every field, including stage, null. */
    suspend fun progress(id: String): Progress = get(shortReadClient, "/api/progress/$id")

    suspend fun nowplaying(): NowPlaying = get(shortReadClient, "/api/nowplaying")

    suspend fun health(): Health = get(shortReadClient, "/api/health")

    suspend fun hifiPlayers(): HifiPlayersResp = get(shortReadClient, "/api/hifi/players")

    suspend fun netcheck(): NetCheck = get(shortReadClient, "/api/netcheck")

    suspend fun netcheckStart(): OkResp = post(shortReadClient, "/api/netcheck")

    suspend fun cancel(): OkResp = post(shortReadClient, "/api/cancel")

    suspend fun stop(): OkResp = post(shortReadClient, "/api/stop")

    suspend fun reconnect(): OkResp = post(shortReadClient, "/api/reconnect")

    // ---- the shelf ----------------------------------------------------------
    // A server that predates these routes answers 404, which `body` turns into an IOException:
    // every caller treats that as "nothing known", which is exactly how the app behaved before.
    // The bodies are encoded rather than interpolated — a title can contain a quote.

    suspend fun shelf(): ShelfPage = get(shortReadClient, "/api/shelf")

    suspend fun setFav(id: String, on: Boolean, snap: ShelfSnap? = null): ShelfResp =
        post(shortReadClient, "/api/shelf/fav", json.encodeToString(FavReq.serializer(), FavReq(id, on, snap)))

    suspend fun setWatched(id: String, on: Boolean, s: Int? = null, e: Int? = null): ShelfResp =
        post(shortReadClient, "/api/shelf/watched", json.encodeToString(WatchedReq.serializer(), WatchedReq(id, on, s, e)))

    /** "Done with this": the server forgets this job's position and stops pinning the title. */
    suspend fun drop(job: String): OkResp =
        post(shortReadClient, "/api/shelf/drop", json.encodeToString(DropReq.serializer(), DropReq(job)))

    /**
     * The lip-sync trim, in ms, positive when the sound should be heard later. Sent the moment the
     * viewer presses the key rather than waiting for the next heartbeat: the server hands it to the
     * audio bridge, which slides its timeline, and the change is audible once the queued audio
     * drains. The heartbeat carries the same value afterwards, so a lost request costs one second.
     */
    suspend fun hifiDelay(ms: Int): OkResp =
        post(shortReadClient, "/api/hifi/delay", """{"ms":$ms}""")

    suspend fun heartbeat(body: HeartbeatBody): HeartbeatResp {
        val payload = json.encodeToString(HeartbeatBody.serializer(), body)
        return post(heartbeatClient, "/api/player/heartbeat", payload)
    }

    sealed class SearchEvent {
        data class Found(val found: SearchFound) : SearchEvent()
        data class Movie(val movie: io.github.superthom196.cinematica.api.Movie) : SearchEvent()
        data class Done(val done: SearchDone) : SearchEvent()
        data class Fail(val err: String?) : SearchEvent()
    }

    /**
     * The search results stream in one-by-one over SSE, rather than waiting for the whole batch:
     * a cold franchise search takes ~25s server-side but the first playable film is ready in about
     * one. Reads the response body line by line on an IO thread; a blank line ends one event.
     */
    fun searchStream(q: String, limit: Int = 24, kind: String = "movie"): Flow<SearchEvent> = callbackFlow {
        val url = "$base/api/search/stream?q=${java.net.URLEncoder.encode(q, "UTF-8")}&limit=$limit" +
            if (kind == "tv") "&kind=tv" else ""
        val request = Request.Builder().url(url).get().build()
        val call = longReadClient.newCall(request)

        // The read loop is a blocking OkHttp call, so it runs on its own child coroutine; awaitClose
        // below just registers the cleanup and suspends until the collector cancels or this job
        // calls close(), which is the correct callbackFlow shape (never awaitClose before the work
        // that is meant to emit).
        val job = launch(Dispatchers.IO) {
            try {
                call.execute().use { response ->
                    val source = response.body.source()
                    val lines = generateSequence { if (source.exhausted()) null else source.readUtf8Line() }
                    for (parsed in parseSseSearchEvents(lines)) {
                        trySend(parsed)
                        if (parsed is SearchEvent.Done || parsed is SearchEvent.Fail) break
                    }
                }
            } catch (_: IOException) {
                // Cancelled by the collector, or the connection dropped mid-stream; either way there
                // is nothing more to emit.
            } finally {
                close()
            }
        }
        awaitClose { call.cancel(); job.cancel() }
    }
}

/**
 * Decodes one complete SSE event ([event]/[data] pair) into a [CinematicaApi.SearchEvent]. Null
 * for an event name the client doesn't know, or a body that fails to decode as that event's shape
 * — the same "silently drop it" outcome [CinematicaApi.searchStream]'s read loop always had.
 */
internal fun decodeSearchEvent(event: String, data: String): CinematicaApi.SearchEvent? = runCatching {
    when (event) {
        "found" -> CinematicaApi.SearchEvent.Found(json.decodeFromString(data))
        "movie" -> CinematicaApi.SearchEvent.Movie(json.decodeFromString(data))
        "done" -> CinematicaApi.SearchEvent.Done(json.decodeFromString(data))
        "fail" -> CinematicaApi.SearchEvent.Fail(json.decodeFromString<Map<String, String>>(data)["err"])
        else -> null
    }
}.getOrNull()

/**
 * Groups raw SSE [lines] into event/data pairs on each blank line and decodes them via
 * [decodeSearchEvent], lazily so a live read loop can send each one the moment it is parsed rather
 * than waiting for the whole response. Stops after the first terminal event ("done" or "fail"),
 * the same early-exit [CinematicaApi.searchStream] applies to a live connection.
 */
internal fun parseSseSearchEvents(lines: Sequence<String>): Sequence<CinematicaApi.SearchEvent> = sequence {
    var event: String? = null
    var data: String? = null
    for (line in lines) {
        when {
            line.startsWith("event:") -> event = line.removePrefix("event:").trim()
            line.startsWith("data:") -> data = line.removePrefix("data:").trim()
            line.isBlank() -> {
                val ev = event; val dt = data
                event = null; data = null
                if (ev != null && dt != null) {
                    val parsed = decodeSearchEvent(ev, dt)
                    if (parsed != null) {
                        yield(parsed)
                        if (parsed is CinematicaApi.SearchEvent.Done || parsed is CinematicaApi.SearchEvent.Fail) return@sequence
                    }
                }
            }
        }
    }
}
