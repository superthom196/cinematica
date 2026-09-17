package io.github.superthom196.cinematica.player

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [LipSync] turns a server sync verdict into a playback nudge. Pure function of its inputs (bar
 * the window, suppression and hold state it keeps between calls), so it is tested the same way as
 * [TrackSelectionTest]: no VLC, no Android, just the decision.
 *
 * Verdicts arrive once a second in real life, so the helpers below feed them at 1 s intervals.
 */
class LipSyncTest {

    /** A [LipSync] with `gen` already established and its 3 s open-suppression elapsed. */
    private fun primed(gen: Int = 1): LipSync {
        val sync = LipSync()
        sync.onOpen(0L)
        sync.step(gen, 0.0, 0, 0L) // first call for this gen: always None, per the rule below.
        return sync
    }

    /** Feed [n] verdicts of [err] a second apart starting at [fromMs]; return the last action. */
    private fun feed(sync: LipSync, err: Double, n: Int, fromMs: Long, gen: Int = 1, rtt: Long = 0): SyncAction {
        var last: SyncAction = SyncAction.None
        repeat(n) { last = sync.step(gen, err, rtt, fromMs + it * 1_000L) }
        return last
    }

    @Test
    fun `nothing is decided until five verdicts are in`() {
        val sync = primed()
        assertEquals(SyncAction.None, feed(sync, 0.3, 4, 3_001L))
        assertEquals(SyncAction.Rate(0.98f), feed(sync, 0.3, 1, 7_001L))
    }

    @Test
    fun `the median ignores a single wild reading`() {
        val sync = primed()
        feed(sync, 0.02, 2, 3_001L)
        sync.step(1, 0.9, 0, 5_001L) // one bad sample
        assertEquals(SyncAction.None, feed(sync, 0.02, 2, 6_001L)) // median 0.02: inside the dead band, rate stays 1.0
    }

    @Test
    fun `picture 150ms behind speeds the TV up by one percent, RTT folded in`() {
        val sync = primed()
        // err_s -0.16, rtt 20ms -> trueErr -0.15, picture behind -> faster.
        assertEquals(SyncAction.Rate(1.01f), feed(sync, -0.16, 5, 3_001L, rtt = 20))
    }

    @Test
    fun `half a second out gets the two percent rate`() {
        val sync = primed()
        assertEquals(SyncAction.Rate(0.98f), feed(sync, 0.5, 5, 3_001L))
    }

    @Test
    fun `a rate is held for eight seconds even if the error moves band`() {
        val sync = primed()
        assertEquals(SyncAction.Rate(0.98f), feed(sync, 0.5, 5, 3_001L)) // applied at 7_001
        assertEquals(SyncAction.None, feed(sync, 0.15, 7, 8_001L)) // up to 14_001: still held
        assertEquals(SyncAction.Rate(0.99f), sync.step(1, 0.15, 0, 15_001L))
    }

    @Test
    fun `back to normal speed is allowed after three seconds`() {
        val sync = primed()
        assertEquals(SyncAction.Rate(1.02f), feed(sync, -0.5, 5, 3_001L)) // applied at 7_001
        assertEquals(SyncAction.None, feed(sync, 0.0, 2, 8_001L)) // 9_001: median still -0.5, and under 3 s anyway
        // 10_001: three of five samples are ~0, the median is inside the dead band, 3 s have passed.
        assertEquals(SyncAction.Rate(1f), sync.step(1, 0.0, 0, 10_001L))
    }

    @Test
    fun `a change of generation suppresses and clears the window`() {
        val sync = primed(gen = 1)
        assertEquals(SyncAction.Rate(0.98f), feed(sync, 0.5, 5, 3_001L))
        // The audio was restarted: whatever rate was chasing the old timeline comes off at once.
        assertEquals(SyncAction.Rate(1f), sync.step(2, 0.5, 0, 20_000L))
        // Five fresh samples are needed again, and the 3 s suppression must pass first.
        assertEquals(SyncAction.None, feed(sync, 0.5, 4, 23_001L, gen = 2))
    }

    @Test
    fun `an error of a few seconds seeks at once and settles for six seconds`() {
        val sync = primed()
        assertEquals(SyncAction.Seek(-1_500), sync.step(1, 1.5, 0, 3_001L))
        // The keyframe dip: the position reads far behind for a while. Ignored.
        assertEquals(SyncAction.None, sync.step(1, -2.5, 0, 5_000L))
        assertEquals(SyncAction.None, sync.step(1, -0.2, 0, 8_900L))
        // Picture 3s behind the audio after the settle: jump forward.
        assertEquals(SyncAction.Seek(3_000), sync.step(1, -3.0, 0, 9_002L))
    }

    @Test
    fun `any error of a second or more seeks, however large, because the server never restarts on drift`() {
        val sync = primed()
        assertEquals(SyncAction.Seek(-30_000), sync.step(1, 30.0, 0, 3_001L))
        val again = primed()
        assertEquals(SyncAction.Seek(45_000), again.step(1, -45.0, 0, 3_001L))
    }

    @Test
    fun `a verdict with an absurd round trip or a non-finite error is ignored`() {
        val sync = primed()
        assertEquals(SyncAction.None, sync.step(1, 5.0, 5_000, 3_001L))
        assertEquals(SyncAction.None, sync.step(1, Double.NaN, 0, 4_001L))
        assertEquals(SyncAction.Seek(-5_000), sync.step(1, 5.0, 0, 5_001L))
    }

    @Test
    fun `a user seek or a resume from pause suppress just like opening a film`() {
        val sync = primed()
        sync.onUserSeek(5_000L)
        assertEquals(SyncAction.None, feed(sync, 0.5, 3, 5_500L))
        assertEquals(SyncAction.Rate(0.98f), feed(sync, 0.5, 5, 8_001L))
        sync.onResume(20_000L)
        assertEquals(SyncAction.None, sync.step(1, -0.5, 0, 20_500L))
    }

    @Test
    fun `reset clears suppression, generation and rate history`() {
        val sync = primed()
        assertEquals(SyncAction.Rate(0.98f), feed(sync, 0.5, 5, 3_001L))
        sync.reset()
        // Same gen as before reset, but reset forgot it, so this is a "first-seen gen" again.
        assertEquals(SyncAction.None, sync.step(1, 0.5, 0, 8_001L))
    }
}
