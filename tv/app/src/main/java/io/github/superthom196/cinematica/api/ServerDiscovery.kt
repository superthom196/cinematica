package io.github.superthom196.cinematica.api

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import kotlinx.serialization.decodeFromString
import okhttp3.OkHttpClient
import okhttp3.Request
import java.net.Inet4Address
import java.net.NetworkInterface
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

private const val TAG = "ServerDiscovery"

/**
 * A Cinematica server is a plain HTTP box on this port; there is nothing to negotiate, so the
 * whole discovery story is "find something that answers GET /api/health on :8090 with JSON that
 * looks like ours".
 */
const val SERVER_PORT = 8090
private const val HEALTH_PATH = "/api/health"

/**
 * Not an mDNS advertisement — just two hostnames a self-hosted box plausibly answers to on an
 * ordinary home LAN: `.local` via Avahi/Bonjour's unicast fallback, `.lan` via a router's DHCP
 * hostname. Worth a direct hit before falling back to a full subnet sweep on the (fairly common)
 * chance someone named their install "cinematica". Order here doesn't matter — via() below is
 * what a caller sees, and "the saved host" always wins that race when it's also one of these two.
 */
private val KNOWN_NAMED_HOSTS = listOf("cinematica.local:$SERVER_PORT", "cinematica.lan:$SERVER_PORT")

/** How often the known-host set (saved + named) is re-probed while the flow stays open. */
private const val KNOWN_HOST_REPROBE_MS = 2_000L

/**
 * Suggested overall lifetime for one discovery run. `discover()` itself never stops on its own —
 * per spec it keeps re-probing known hosts until the collector cancels — so this is only a value
 * for the caller to `delay(DISCOVERY_WINDOW_MS)` and then cancel the collecting job, the same way
 * MaDiscovery.WINDOW_MS is used from AppViewModel in the MATV app.
 */
const val DISCOVERY_WINDOW_MS = 15_000L

// The sweep hits up to 254 dead addresses per pass, so it needs to fail fast and wide: a short
// connect timeout (most non-servers just never ACK) and a gate so a slow TV NIC or router doesn't
// choke on 254 simultaneous SYNs.
private const val SWEEP_CONCURRENCY = 32
private const val SWEEP_CONNECT_TIMEOUT_MS = 400L
private const val SWEEP_READ_TIMEOUT_MS = 1_500L

// The known-host loop, by contrast, is only ever a couple of connections at a time and wants to
// tolerate a Pi that is mid-boot without giving up in the middle of a 2s cycle.
private const val KNOWN_CONNECT_TIMEOUT_MS = 800L
private const val KNOWN_READ_TIMEOUT_MS = 2_000L

// If the link is still coming up (TV just left standby) there is no IPv4 address to sweep from
// yet. Wait for one, but leave the bulk of the 15s window for the sweep itself.
private const val IPV4_WAIT_BUDGET_MS = 6_000L
private const val IPV4_WAIT_STEP_MS = 750L

/**
 * One server found on the LAN, however it was found.
 *
 * @param host "host:port" exactly as it belongs in the settings screen's server field — typing
 *   this back in reproduces the connection, which is why it's kept separate from [baseUrl].
 * @param baseUrl "http://host:port", ready to hand to [CinematicaApi] / [normaliseBaseUrl].
 * @param via "saved" (the host already in settings answered), "name" (one of the well-known
 *   hostnames answered) or "scan" (found by sweeping the subnet).
 */
data class FoundServer(
    val host: String,
    val baseUrl: String,
    val movies: Int?,
    val imdbTitles: Int?,
    val tvMsg: String?,
    val via: String,
)

/**
 * Turns a `/api/health` response body into a [FoundServer], or null if it plainly isn't one.
 *
 * Pure and Android-free on purpose: this is the one function that decides "is this actually a
 * Cinematica server", so it needs to be trivially unit-testable against recorded/hand-written
 * bodies without spinning up a fake HTTP server. A body that fails to parse as JSON, or parses
 * but has no `providers` key, is treated as "something else answered on this host" rather than as
 * an error — routers, printers and IoT junk all happily answer plain HTTP on random ports.
 */
fun parseHealth(host: String, baseUrl: String, via: String, body: String): FoundServer? {
    val health = runCatching { json.decodeFromString<Health>(body) }.getOrNull() ?: return null
    // `providers` is present, unconditionally, on every real /api/health response (configured or
    // still needing setup) and on nothing else we're likely to meet on a home LAN, so it's the
    // cheapest reliable "this is our server" signal without hard-coding a magic string.
    if (health.providers == null) return null
    return FoundServer(
        host = host,
        baseUrl = baseUrl,
        movies = health.movies,
        imdbTitles = health.imdb_titles,
        tvMsg = health.tv_msg,
        via = via,
    )
}

/**
 * Every host address in [ownIpv4]'s /24 worth knocking on: 1..254 minus the network address
 * (.0), the broadcast address (.255) — both already outside that range — and [ownIpv4] itself,
 * since scanning yourself wastes a slot in the concurrency gate for a connection that OkHttp may
 * or may not even complete cleanly on loopback-ish routing.
 *
 * [prefix] defaults to the first three octets of [ownIpv4]; it's a separate parameter (rather
 * than always derived) so the caller can sweep a /24 other than the one implied by the device's
 * own address if that's ever needed, and so this function can be unit-tested with made-up values.
 */
fun candidateAddresses(ownIpv4: String, prefix: String = ownIpv4.substringBeforeLast('.')): List<String> =
    (1..254).map { "$prefix.$it" }.filterNot { it == ownIpv4 }

/** Sweeps never scan more than a /22: a router handing out a wider lease (/16, /20, ...) would
 * otherwise turn into tens of thousands of probes. A [prefixLength] narrower than that (a /24,
 * /28, ...) is swept as reported; anything wider is clamped to the /MIN_SWEEP_PREFIX block that
 * contains [ownIp]. */
private const val MIN_SWEEP_PREFIX = 22

fun candidateAddressesForPrefix(ownIp: String, prefixLength: Int): List<String> {
    val ownInt = ipv4ToInt(ownIp) ?: return emptyList()
    val hostBits = 32 - maxOf(prefixLength, MIN_SWEEP_PREFIX)
    if (hostBits <= 1) return emptyList() // /31 or /32: no room for another host
    val network = ownInt and (-1 shl hostBits)
    return (1 until (1 shl hostBits) - 1).map { intToIpv4(network + it) }.filterNot { it == ownIp }
}

private fun ipv4ToInt(ip: String): Int? {
    val nums = ip.split('.').map { it.toIntOrNull() ?: return null }
    if (nums.size != 4 || nums.any { it !in 0..255 }) return null
    return nums.fold(0) { acc, n -> (acc shl 8) or n }
}

private fun intToIpv4(value: Int): String =
    "${(value ushr 24) and 0xFF}.${(value ushr 16) and 0xFF}.${(value ushr 8) and 0xFF}.${value and 0xFF}"

/** saved beats a well-known hostname beats a bare scan hit — a name someone typed in is trusted more than an IP a sweep happened to find. */
fun viaRank(via: String): Int = when (via) {
    "saved" -> 0
    "name" -> 1
    "scan" -> 2
    else -> 3
}

/**
 * Numeric key for "then by IP": a dotted-quad host sorts by its actual address value, so a scan
 * of 192.168.0.0/24 lists .2 before .10 instead of lexicographically ("10" < "2"). A hostname
 * (the saved/name tiers are usually hostnames, not IPs) has no such value, so it sorts after every
 * real IP in its tier and falls back to plain string order via the comparator's last key.
 */
private fun ipSortKey(host: String): Long {
    val octets = host.substringBefore(':').split('.')
    if (octets.size != 4) return Long.MAX_VALUE
    val nums = octets.map { it.toIntOrNull() ?: return Long.MAX_VALUE }
    if (nums.any { it !in 0..255 }) return Long.MAX_VALUE
    return nums.fold(0L) { acc, n -> (acc shl 8) or n.toLong() }
}

/** saved -> name -> scan, then by IP, then by the raw host string as a final, fully deterministic tiebreaker. */
val FOUND_SERVER_ORDER: Comparator<FoundServer> =
    compareBy<FoundServer> { viaRank(it.via) }.thenBy { ipSortKey(it.host) }.thenBy { it.host }

/** Convenience wrapper around [FOUND_SERVER_ORDER] so the ordering can be exercised directly in tests. */
fun sortFoundServers(servers: List<FoundServer>): List<FoundServer> = servers.sortedWith(FOUND_SERVER_ORDER)

/**
 * The identity a server actually has: its address and port, whatever name was used to reach it.
 *
 * One box can answer to its saved hostname, to one of the [KNOWN_NAMED_HOSTS], and to its bare IP
 * when the sweep finds it, and keying the found set on the host string would list that single
 * machine three times. The resolved address is the same for all three, so it is what the set is
 * keyed on.
 */
fun serverKey(host: String, ip: String): String {
    val port = host.substringAfterLast(':', "").toIntOrNull() ?: SERVER_PORT
    return "$ip:$port"
}

/**
 * Which of two ways of reaching the same machine to show. saved beats name beats scan — a name
 * someone typed is friendlier than one this file knows and far friendlier than a bare IP — and at
 * equal rank the newer probe wins, so a re-probe refreshes the film count in place.
 */
fun preferredServer(previous: FoundServer, next: FoundServer): FoundServer =
    if (viaRank(next.via) <= viaRank(previous.via)) next else previous

/**
 * Finds Cinematica servers on the LAN.
 *
 * There is no mDNS advertisement to listen for (unlike Music Assistant's `_mass._tcp`), so this
 * is deliberately simpler than MaDiscovery in the MATV project: no NsdManager listener, just known
 * hosts re-probed on a timer plus a one-shot subnet sweep, both feeding the same de-duplicated,
 * growing list.
 *
 * HOOK — if the server ever grows a `_cinematica._tcp` mDNS/NSD advertisement, this is where a
 * listener would go: an `NsdManager.DiscoveryListener` that resolves each hit and calls
 * `offer(FoundServer(..., via = "name"))`, launched here alongside [knownJob] and torn down in the
 * same `awaitClose`. See `MaDiscovery.kt` in the MATV project for the exact shape to copy.
 *
 * @param savedHost the host currently in settings, if any ("host:port"); probed first and most
 *   trusted (`via = "saved"`).
 * @param client shared OkHttp client to derive per-purpose timeouts from ([SWEEP_CONNECT_TIMEOUT_MS]
 *   etc.); this function never touches its default timeouts, only clones with `newBuilder()`.
 */
fun discover(context: Context, savedHost: String?, client: OkHttpClient): Flow<List<FoundServer>> = callbackFlow {
    // Touched from the known-host loop and from up to 32 sweep workers at once; a plain HashMap
    // would race. Keyed by the server's RESOLVED address (see [serverKey]) so the one machine
    // appears once however many names reach it, and a re-probe updates its entry in place.
    val found = ConcurrentHashMap<String, FoundServer>()

    fun offer(key: String, server: FoundServer) {
        var previous: FoundServer? = null
        val chosen = found.compute(key) { _, existing ->
            previous = existing
            if (existing == null) server else preferredServer(existing, server)
        }
        // An identical re-probe result, or a less friendly way of reaching a machine already
        // listed: nothing new to tell the collector.
        if (chosen == previous) return
        trySend(sortFoundServers(found.values.toList()))
    }

    val sweepClient = client.newBuilder()
        .connectTimeout(SWEEP_CONNECT_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .readTimeout(SWEEP_READ_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .build()
    val knownClient = client.newBuilder()
        .connectTimeout(KNOWN_CONNECT_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .readTimeout(KNOWN_READ_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        .build()

    // --- known candidates: the saved host plus the two well-known hostnames above ---
    // Kept alive for the whole life of the flow, not just an initial pass: the usual reason to be
    // discovering at all is that the TV just woke from standby and the network — or the server
    // itself — is still coming up, so a server that only starts answering after a few seconds
    // must still be found.
    val knownJob = launch(Dispatchers.IO) {
        val candidates = LinkedHashSet<String>().apply {
            savedHost?.trim()?.takeIf { it.isNotEmpty() }?.let(::add)
            addAll(KNOWN_NAMED_HOSTS)
        }
        while (isActive) {
            for (host in candidates) {
                val via = if (host == savedHost) "saved" else "name"
                probeHealth(knownClient, host, via)?.let { (key, server) -> offer(key, server) }
            }
            delay(KNOWN_HOST_REPROBE_MS)
        }
    }

    // --- one-shot /24 sweep ---
    val sweepJob = launch(Dispatchers.IO) {
        val (ownIp, prefixLength) = awaitIpv4(context) ?: run {
            Log.i(TAG, "no local IPv4 address before the scan window closed; skipping the sweep")
            return@launch
        }
        val addresses = candidateAddressesForPrefix(ownIp, prefixLength)
        Log.i(TAG, "sweeping ${addresses.size} hosts (/$prefixLength, capped at /$MIN_SWEEP_PREFIX) from $ownIp")
        val gate = Semaphore(SWEEP_CONCURRENCY)
        // coroutineScope, not a bare loop of launch{}: this suspends until every one of the (up
        // to) 254 probes has finished or been cancelled, so cancelling sweepJob can never leak a
        // probe coroutine past the point discover()'s caller thinks the sweep is done.
        coroutineScope {
            for (address in addresses) {
                launch {
                    gate.withPermit {
                        probeHealth(sweepClient, "$address:$SERVER_PORT", "scan")
                            ?.let { (key, server) -> offer(key, server) }
                    }
                }
            }
        }
    }

    awaitClose {
        knownJob.cancel()
        sweepJob.cancel()
    }
}.flowOn(Dispatchers.IO)

/**
 * GET [HEALTH_PATH] on [host] and decode it, or null on any failure (refused, timed out, not our
 * server). The blocking OkHttp call and the JSON decode are dispatched separately on purpose: the
 * network read belongs on IO, the CPU-bound decode belongs on Default, matching how CinematicaApi
 * splits the same two concerns.
 */
private suspend fun probeHealth(client: OkHttpClient, host: String, via: String): Pair<String, FoundServer>? {
    val baseUrl = normaliseBaseUrl(host)
    val body = withContext(Dispatchers.IO) {
        runCatching {
            val request = Request.Builder().url("$baseUrl$HEALTH_PATH").get().build()
            client.newCall(request).execute().use { it.body.string() }
        }.getOrNull()
    } ?: return null
    val server = withContext(Dispatchers.Default) { parseHealth(host, baseUrl, via, body) } ?: return null
    return resolvedKey(host) to server
}

/**
 * "<ip>:<port>" for [host]. The lookup is a blocking DNS call, so it belongs on IO; a name that
 * answered HTTP a moment ago will normally resolve from cache. If it somehow does not, the host
 * string itself is the key — that is the old behaviour, which lists a machine twice at worst
 * rather than merging two different ones.
 */
private suspend fun resolvedKey(host: String): String = withContext(Dispatchers.IO) {
    val name = host.substringBefore(':')
    val ip = runCatching { java.net.InetAddress.getByName(name).hostAddress }.getOrNull()
    if (ip == null) host.lowercase() else serverKey(host, ip)
}

/** Polls for a local IPv4 address, giving the link up to [IPV4_WAIT_BUDGET_MS] to come up before giving up on the sweep for this run. */
private suspend fun awaitIpv4(context: Context): Pair<String, Int>? {
    var waited = 0L
    while (waited < IPV4_WAIT_BUDGET_MS) {
        localIpv4(context)?.let { return it }
        delay(IPV4_WAIT_STEP_MS)
        waited += IPV4_WAIT_STEP_MS
    }
    return localIpv4(context)
}

/** A VPN/tunnel interface's own "LAN" is a point-to-point tunnel, not a sweepable subnet. */
private fun isTunnelIface(name: String?): Boolean =
    name != null && (name.startsWith("tun") || name.startsWith("wg") || name.startsWith("tap"))

/**
 * The device's own IPv4 address and its real prefix length, or null if there isn't one yet.
 * Prefers the active network's LinkProperties (works even when NetworkInterface enumeration is
 * flaky right after a Wi-Fi reconnect on some TV boxes); falls back to raw interface enumeration
 * otherwise — the same two-step MaDiscovery.localIpv4Prefixes() uses in the MATV project. Either
 * way, a tunnel interface is skipped rather than swept.
 */
private fun localIpv4(context: Context): Pair<String, Int>? {
    val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
    val props: LinkProperties? = cm?.activeNetwork?.let { cm.getLinkProperties(it) }
    val fromProps = if (isTunnelIface(props?.interfaceName)) null else
        props?.linkAddresses.orEmpty()
            .mapNotNull { la -> (la.address as? Inet4Address)?.hostAddress?.let { ip -> ip to la.prefixLength } }
            .firstOrNull()
    return fromProps ?: runCatching {
        NetworkInterface.getNetworkInterfaces().toList()
            .filter { it.isUp && !it.isLoopback && !isTunnelIface(it.name) }
            .flatMap { it.interfaceAddresses }
            .firstOrNull { it.address is Inet4Address }
            ?.let { ia -> (ia.address as Inet4Address).hostAddress to ia.networkPrefixLength.toInt() }
    }.getOrNull()
}
