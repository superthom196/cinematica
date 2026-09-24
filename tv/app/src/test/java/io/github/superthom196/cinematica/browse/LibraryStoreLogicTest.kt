package io.github.superthom196.cinematica.browse

import io.github.superthom196.cinematica.api.Movie
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Test

/**
 * Covers [applyChannelPatch], the pure half of [LibraryStore.patchChannel]. [LibraryStore] itself
 * needs a real [io.github.superthom196.cinematica.data.Prefs] (so an [android.content.Context]),
 * which nothing in this module's plain-JVM tests has, so the list-restructuring logic is tested
 * through this extracted function instead.
 */
class LibraryStoreLogicTest {

    private fun channel(id: String, followed: Boolean, new: Int = 0) =
        Movie(id = id, kind = "channel", followed = followed, new = new)

    @Test
    fun `a follow moves the channel from popular into movies`() {
        val state = LibraryState(
            movies = listOf(channel("a", followed = true)),
            popular = listOf(channel("b", followed = false, new = 3)),
        )
        val next = applyChannelPatch(state, "b") { it.copy(followed = true, new = 0) }

        assertEquals(listOf("a", "b"), next.movies.map { it.id })
        assertEquals(emptyList<String>(), next.popular.map { it.id })
        assertEquals(true, next.movies.last().followed)
    }

    @Test
    fun `an unfollow moves the channel from movies into popular`() {
        val state = LibraryState(
            movies = listOf(channel("a", followed = true), channel("b", followed = true)),
            popular = emptyList(),
        )
        val next = applyChannelPatch(state, "a") { it.copy(followed = false) }

        assertEquals(listOf("b"), next.movies.map { it.id })
        assertEquals(listOf("a"), next.popular.map { it.id })
        assertEquals(false, next.popular.single().followed)
    }

    @Test
    fun `clearing NEW in place does not move the channel between lists`() {
        val state = LibraryState(movies = listOf(channel("a", followed = true, new = 5)))
        val next = applyChannelPatch(state, "a") { it.copy(new = 0) }

        assertEquals(listOf("a"), next.movies.map { it.id })
        assertEquals(0, next.movies.single().new)
    }

    @Test
    fun `an id in neither list leaves the state untouched`() {
        val state = LibraryState(movies = listOf(channel("a", followed = true)))
        val next = applyChannelPatch(state, "missing") { it.copy(new = 0) }

        assertSame(state, next)
    }
}
