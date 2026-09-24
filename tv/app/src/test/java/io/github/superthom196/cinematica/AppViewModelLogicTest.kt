package io.github.superthom196.cinematica

import io.github.superthom196.cinematica.player.PlayStatus
import io.github.superthom196.cinematica.session.PlayerReport
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Covers the pure decision logic pulled out of [AppViewModel]: the heartbeat's outgoing state
 * report, the resume-wait loop's branching, and the PIN throttle. Everything else in
 * [AppViewModel] needs an [android.app.Application], real coroutine dispatchers or the embedded
 * player, none of which a plain JVM unit test has.
 */
class AppViewModelLogicTest {

    // ---- buildPlayerReport ------------------------------------------------------

    @Test
    fun `opening and buffering both report buffering with the position so far`() {
        val opening = buildPlayerReport(PlayStatus.Opening, 1.5, 90.0, "job1", "Heat", false, false, false)
        val buffering = buildPlayerReport(PlayStatus.Buffering(0.4f), 1.5, 90.0, "job1", "Heat", false, false, false)

        assertEquals(PlayerReport("buffering", "job1", "Heat", 1.5, 90.0), opening)
        assertEquals(PlayerReport("buffering", "job1", "Heat", 1.5, 90.0), buffering)
    }

    @Test
    fun `playing and paused report their state and position, unless a resume is holding`() {
        assertEquals(
            PlayerReport("playing", "job1", "Heat", 10.0, 90.0),
            buildPlayerReport(PlayStatus.Playing, 10.0, 90.0, "job1", "Heat", resumeHolding = false, endedReported = false, errorReported = false),
        )
        assertEquals(
            PlayerReport("paused", "job1", "Heat", 10.0, 90.0),
            buildPlayerReport(PlayStatus.Paused, 10.0, 90.0, "job1", "Heat", resumeHolding = false, endedReported = false, errorReported = false),
        )
        // Held for a resume: no position is reported, because the player is sitting on 0:00 and
        // that is not where the viewer actually got to.
        assertEquals(
            PlayerReport("buffering", "job1", "Heat"),
            buildPlayerReport(PlayStatus.Playing, 0.0, 90.0, "job1", "Heat", resumeHolding = true, endedReported = false, errorReported = false),
        )
        assertEquals(
            PlayerReport("buffering", "job1", "Heat"),
            buildPlayerReport(PlayStatus.Paused, 0.0, 90.0, "job1", "Heat", resumeHolding = true, endedReported = false, errorReported = false),
        )
    }

    @Test
    fun `ended is reported once, then idle`() {
        assertEquals(
            PlayerReport("ended", "job1", "Heat", 90.0, 90.0),
            buildPlayerReport(PlayStatus.Ended, 90.0, 90.0, "job1", "Heat", resumeHolding = false, endedReported = false, errorReported = false),
        )
        assertEquals(
            PlayerReport("idle"),
            buildPlayerReport(PlayStatus.Ended, 90.0, 90.0, "job1", "Heat", resumeHolding = false, endedReported = true, errorReported = false),
        )
    }

    @Test
    fun `error carries its message once, then idle`() {
        assertEquals(
            PlayerReport("error", "job1", "Heat", err = "no route to the server"),
            buildPlayerReport(PlayStatus.Error("no route to the server"), null, null, "job1", "Heat", resumeHolding = false, endedReported = false, errorReported = false),
        )
        assertEquals(
            PlayerReport("idle"),
            buildPlayerReport(PlayStatus.Error("no route to the server"), null, null, "job1", "Heat", resumeHolding = false, endedReported = false, errorReported = true),
        )
    }

    @Test
    fun `idle status is buffering while a job is pending, and idle once it clears`() {
        assertEquals(
            PlayerReport("buffering", "job1", "Heat"),
            buildPlayerReport(PlayStatus.Idle, null, null, "job1", "Heat", resumeHolding = false, endedReported = false, errorReported = false),
        )
        assertEquals(
            PlayerReport("idle"),
            buildPlayerReport(PlayStatus.Idle, null, null, null, null, resumeHolding = false, endedReported = false, errorReported = false),
        )
    }

    // ---- resumeStep ---------------------------------------------------------------

    @Test
    fun `resumeStep stops the loop once the player is no longer active, whatever else is true`() {
        assertEquals(ResumeAction.STOP_LOOP, resumeStep(active = false, isLive = true, seekable = true, lengthMs = 999_999L, targetMs = 1000L, marginMs = 10_000L))
    }

    @Test
    fun `resumeStep seeks a non-live (already playable) media the moment it is seekable, else waits silently`() {
        assertEquals(ResumeAction.SEEK, resumeStep(active = true, isLive = false, seekable = true, lengthMs = 0L, targetMs = 5000L, marginMs = 10_000L))
        assertEquals(ResumeAction.SILENT_WAIT, resumeStep(active = true, isLive = false, seekable = false, lengthMs = 0L, targetMs = 5000L, marginMs = 10_000L))
    }

    @Test
    fun `resumeStep seeks a live conversion once its length clears the target by the margin`() {
        // lengthMs strictly greater than targetMs + marginMs.
        assertEquals(ResumeAction.SEEK_LIVE, resumeStep(active = true, isLive = true, seekable = false, lengthMs = 20_001L, targetMs = 5000L, marginMs = 15_000L))
    }

    @Test
    fun `resumeStep holds while the live conversion has not cleared the margin, including exactly at it`() {
        // Exactly at the margin (not strictly past it) still holds -- the comparison is a strict >.
        assertEquals(ResumeAction.HOLD, resumeStep(active = true, isLive = true, seekable = false, lengthMs = 20_000L, targetMs = 5000L, marginMs = 15_000L))
        assertEquals(ResumeAction.HOLD, resumeStep(active = true, isLive = true, seekable = false, lengthMs = 0L, targetMs = 5000L, marginMs = 15_000L))
    }

    // ---- evaluatePinAttempt -----------------------------------------------------

    @Test
    fun `a correct pin resets the failure count and clears any throttle`() {
        val result = evaluatePinAttempt(matched = true, failuresBefore = 4, nowMs = 1_000_000L, maxTries = 5, waitMs = 30_000L)
        assertEquals(PinAttempt(failuresAfter = 0, retryAt = 0L), result)
    }

    @Test
    fun `a wrong pin under the limit just counts, and leaves any existing throttle alone`() {
        val result = evaluatePinAttempt(matched = false, failuresBefore = 1, nowMs = 1_000_000L, maxTries = 5, waitMs = 30_000L)
        assertEquals(PinAttempt(failuresAfter = 2, retryAt = null), result)
    }

    @Test
    fun `a wrong pin that reaches the limit throttles and resets the counter`() {
        val result = evaluatePinAttempt(matched = false, failuresBefore = 4, nowMs = 1_000_000L, maxTries = 5, waitMs = 30_000L)
        assertEquals(PinAttempt(failuresAfter = 0, retryAt = 1_030_000L), result)
    }
}
