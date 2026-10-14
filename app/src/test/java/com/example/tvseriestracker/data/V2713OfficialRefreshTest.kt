package com.example.tvseriestracker.data

import java.io.IOException
import java.time.Instant
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.yield
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Assert.*
import org.junit.Test

class V2713OfficialRefreshTest {
    @Test fun samePayloadRetriesForRequestedIdThenFreshFactUpdatesAndStops() = runBlocking {
        val store = Store(); store.replace(payload(T1), Instant.EPOCH)
        val visibleFact = async { store.cache.first { snapshot -> snapshot.series.any { it.tmdbId == 90296 } } }
        val calls = AtomicInteger()
        val repository = repo(store) { when (calls.incrementAndGet()) {
            1, 2 -> json(T1)
            else -> json(T2, row(90296))
        } }
        val coordinator = coordinator(repository)
        coordinator.setActive(true); coordinator.requested(90296)
        assertEquals(3, calls.get())
        assertEquals(T2, store.snapshot().generatedAt)
        assertEquals(90296, store.snapshot().series.single().tmdbId)
        assertEquals(90296, visibleFact.await().series.single().tmdbId)
        assertEquals(listOf(30_000L, 60_000L, 120_000L), coordinator.delays)
    }

    @Test fun newFactOnFirstFollowUpStopsWithoutMoreFetches() = runBlocking {
        val store = Store(); val calls = AtomicInteger()
        val coordinator = coordinator(repo(store) { calls.incrementAndGet(); json(T2, row(90296)) })
        coordinator.setActive(true); coordinator.requested(90296)
        assertEquals(1, calls.get()); assertEquals(1, store.snapshot().series.size)
    }

    @Test fun allStaleResponsesStopAfterThreeAdditionalAttempts() = runBlocking {
        val store = Store(); store.replace(payload(T1), Instant.EPOCH)
        val calls = AtomicInteger()
        val coordinator = coordinator(repo(store) { calls.incrementAndGet(); json(T1) })
        coordinator.setActive(true); coordinator.requested(90296)
        assertEquals(3, calls.get()); assertTrue(store.snapshot().series.isEmpty())
    }

    @Test fun olderPayloadCannotReplaceCache() = runBlocking {
        val store = Store(); store.replace(payload(T3, row(90296)), Instant.EPOCH)
        val repository = repo(store) { json(T2) }
        assertFalse(repository.refresh())
        assertEquals(T3, store.snapshot().generatedAt)
        assertEquals(90296, store.snapshot().series.single().tmdbId)
    }

    @Test fun newerThenLaterOlderPayloadLeavesNewestCache() = runBlocking {
        val store = Store(); store.replace(payload(T1), Instant.EPOCH)
        assertTrue(repo(store) { json(T3, row(90296)) }.refresh())
        assertFalse(repo(store) { json(T2) }.refresh())
        assertEquals(T3, store.snapshot().generatedAt)
        assertEquals(90296, store.snapshot().series.single().tmdbId)
    }

    @Test fun overlappingNewerThenOlderRefreshesShareOneFetch() = runBlocking {
        val store = Store(); store.replace(payload(T1), Instant.EPOCH)
        val calls = AtomicInteger(); val response = CompletableDeferred<String>()
        val repository = repo(store) { calls.incrementAndGet(); response.await() }
        val first = async { repository.refresh() }
        yield()
        val second = async { repository.refresh() }
        response.complete(json(T3, row(90296)))
        assertTrue(first.await()); assertTrue(second.await())
        assertEquals(1, calls.get()); assertEquals(T3, store.snapshot().generatedAt)
    }

    @Test fun networkErrorPreservesVerifiedCache() = runBlocking {
        val store = Store(); store.replace(payload(T1, row(153312)), Instant.EPOCH)
        val before = store.snapshot()
        try { repo(store) { throw IOException("offline") }.refresh(); fail("expected failure") }
        catch (_: IOException) { }
        assertEquals(before, store.snapshot())
    }

    @Test fun timeoutPreservesVerifiedCache() = runBlocking {
        val store = Store(); store.replace(payload(T1, row(153312)), Instant.EPOCH)
        val before = store.snapshot()
        try { repo(store) { throw IOException("timeout") }.refresh(); fail("expected timeout") }
        catch (_: IOException) { }
        assertEquals(before, store.snapshot())
    }

    @Test fun oversizedRemoteBodyIsRejectedAndCacheRemainsUsable() = runBlocking {
        val store = Store(); store.replace(payload(T1, row(153312)), Instant.EPOCH)
        val before = store.snapshot()
        val oversized = okio.Buffer().write(ByteArray(HttpOfficialRemoteSource.MAX_OFFICIAL_DATA_BYTES.toInt() + 1))
        try {
            repo(store) { readBoundedOfficialData(oversized, -1) }.refresh()
            fail("expected size rejection")
        } catch (_: IOException) { }
        assertEquals(before, store.snapshot())
    }

    @Test fun invalidJsonPreservesVerifiedCache() = runBlocking {
        val store = Store(); store.replace(payload(T1, row(153312)), Instant.EPOCH)
        val before = store.snapshot()
        try { repo(store) { "{" }.refresh(); fail("expected failure") }
        catch (_: Exception) { }
        assertEquals(before, store.snapshot())
    }

    @Test fun schemaMismatchPreservesVerifiedCache() = runBlocking {
        val store = Store(); store.replace(payload(T1, row(153312)), Instant.EPOCH)
        val before = store.snapshot()
        try { repo(store) { json(T2).replace("schemaVersion\":1", "schemaVersion\":2") }.refresh(); fail("expected failure") }
        catch (_: IllegalArgumentException) { }
        assertEquals(before, store.snapshot())
    }

    @Test fun malformedGeneratedAtPreservesVerifiedCache() = runBlocking {
        val store = Store(); store.replace(payload(T1, row(153312)), Instant.EPOCH)
        val before = store.snapshot()
        try { repo(store) { json(T2).replace(T2.toString(), "not-a-time") }.refresh(); fail("expected invalid generatedAt") }
        catch (_: Exception) { }
        assertEquals(before, store.snapshot())
    }

    @Test fun manualSamePayloadReportsAlreadyCurrent() = runBlocking {
        val store = Store(); store.replace(payload(T1, row(153312)), Instant.EPOCH)
        assertFalse(repo(store) { json(T1) }.refresh())
        assertEquals(153312, store.snapshot().series.single().tmdbId)
    }

    @Test fun manualNewerPayloadUpdatesCache() = runBlocking {
        val store = Store(); store.replace(payload(T1), Instant.EPOCH)
        assertTrue(repo(store) { json(T2, row(90296)) }.refresh())
        assertEquals(90296, store.snapshot().series.single().tmdbId)
    }

    @Test fun ordinaryStartupUsesOnlyItsSingleStalenessRefresh() = runBlocking {
        val store = Store()
        store.replace(payload(T1), Instant.EPOCH)
        store.touch(Instant.EPOCH)
        val calls = AtomicInteger()
        repo(store, now = { Instant.EPOCH.plusSeconds(24 * 60 * 60 + 1) }) {
            calls.incrementAndGet(); json(T1)
        }.refreshIfStale()
        assertEquals(1, calls.get())
    }

    @Test fun unrelatedVerifiedIdDoesNotStopRequestedIdRetry() = runBlocking {
        val store = Store(); val calls = AtomicInteger()
        val coordinator = coordinator(repo(store) { calls.incrementAndGet(); json(T2, row(153312)) })
        coordinator.setActive(true); coordinator.requested(90296)
        assertEquals(3, calls.get()); assertEquals(153312, store.snapshot().series.single().tmdbId)
    }

    @Test fun leavingActiveLifecycleCancelsBeforeNextRequest() = runBlocking {
        val store = Store(); val calls = AtomicInteger()
        lateinit var coordinator: RequestedOfficialRefresh
        coordinator = RequestedOfficialRefresh(CoroutineScope(SupervisorJob() + Dispatchers.Unconfined),
            repo(store) { calls.incrementAndGet(); json(T2, row(90296)) },
            wait = { coordinator.setActive(false) })
        coordinator.setActive(true); coordinator.requested(90296)
        assertEquals(0, calls.get())
    }

    @Test fun cancellationExceptionPropagatesFromRefresh() = runBlocking {
        val store = Store()
        try { repo(store) { throw kotlinx.coroutines.CancellationException("cancelled") }.refresh(); fail("expected cancellation") }
        catch (_: kotlinx.coroutines.CancellationException) { }
        assertTrue(store.snapshot().series.isEmpty())
    }

    @Test fun lifecycleStopDropsPendingBatchWithoutRestartingBudget() = runBlocking {
        val store = Store(); val calls = AtomicInteger()
        val coordinator = coordinator(repo(store) { calls.incrementAndGet(); json(T2, row(90296), row(111110)) })
        coordinator.setActive(true); coordinator.setActive(false); coordinator.requested(90296)
        yield()
        assertEquals(0, calls.get())
    }

    @Test fun canceledOldBatchCannotConsumeRestartedBatchAttemptBudget() = runBlocking {
        val store = Store(); val calls = AtomicInteger()
        val oldFetchStarted = CompletableDeferred<Unit>()
        val releaseOldCancellation = CompletableDeferred<Unit>()
        var waits = 0
        val repository = repo(store) {
            if (calls.incrementAndGet() == 1) {
                oldFetchStarted.complete(Unit)
                try { awaitCancellation() }
                finally { withContext(NonCancellable) { releaseOldCancellation.await() } }
            } else json(T1)
        }
        val coordinator = RequestedOfficialRefresh(CoroutineScope(SupervisorJob() + Dispatchers.Unconfined),
            repository, wait = { waits++ })
        coordinator.setActive(true); coordinator.requested(90296)
        oldFetchStarted.await()
        coordinator.setActive(false)
        coordinator.setActive(true); coordinator.requested(111110)
        releaseOldCancellation.complete(Unit)
        withTimeout(2_000) { while (calls.get() < 4) yield() }
        assertEquals("old canceled fetch plus three new-batch attempts", 4, calls.get())
        assertEquals("one old wait plus three new waits", 4, waits)
    }

    @Test fun simultaneousRequestedIdsShareOneBoundedRefreshSeries() = runBlocking {
        val store = Store(); val calls = AtomicInteger()
        val gate = CompletableDeferred<Unit>()
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Unconfined)
        val coordinator = RequestedOfficialRefresh(scope,
            repo(store) { calls.incrementAndGet(); json(T2, row(90296), row(111110)) },
            wait = { gate.await() })
        coordinator.setActive(true); coordinator.requested(90296); coordinator.requested(111110)
        gate.complete(Unit)
        assertEquals(1, calls.get())
        assertEquals(setOf(90296, 111110), store.snapshot().series.map { it.tmdbId }.toSet())
    }

    @Test fun lateRequestedIdJoinsRemainingBatchAttemptsWithoutReset() = runBlocking {
        val store = Store(); val calls = AtomicInteger()
        val firstWait = CompletableDeferred<Unit>(); val secondWait = CompletableDeferred<Unit>()
        val reachedSecondWait = CompletableDeferred<Unit>()
        var waitIndex = 0
        val coordinator = RequestedOfficialRefresh(CoroutineScope(SupervisorJob() + Dispatchers.Unconfined),
            repo(store) { calls.incrementAndGet(); json(T1) }, wait = {
                when (++waitIndex) {
                    1 -> firstWait.await()
                    2 -> { reachedSecondWait.complete(Unit); secondWait.await() }
                }
            })
        coordinator.setActive(true); coordinator.requested(90296)
        firstWait.complete(Unit)
        reachedSecondWait.await()
        coordinator.requested(111110)
        secondWait.complete(Unit)
        assertEquals(3, calls.get())
        assertEquals(3, waitIndex)
    }

    @Test fun manualRefreshThatSuppliesFactDuringWaitStopsFollowUp() = runBlocking {
        val store = Store(); val calls = AtomicInteger(); val gate = CompletableDeferred<Unit>()
        val repository = repo(store) { calls.incrementAndGet(); json(T2, row(90296)) }
        val coordinator = RequestedOfficialRefresh(CoroutineScope(SupervisorJob() + Dispatchers.Unconfined),
            repository, wait = { gate.await() })
        coordinator.setActive(true); coordinator.requested(90296)
        assertTrue(repository.refresh("manual"))
        gate.complete(Unit)
        assertEquals(1, calls.get())
        assertEquals(90296, store.snapshot().series.single().tmdbId)
    }

    private fun coordinator(repository: OfficialDataRepository): TestCoordinator {
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Unconfined)
        val delays = mutableListOf<Long>()
        return TestCoordinator(RequestedOfficialRefresh(scope, repository, wait = { delays += it }), delays)
    }

    private data class TestCoordinator(val value: RequestedOfficialRefresh, val delays: MutableList<Long>) {
        fun setActive(active: Boolean) = value.setActive(active)
        fun requested(id: Int) = value.requested(id)
    }

    private fun repo(store: Store, now: () -> Instant = { T3 }, fetch: suspend () -> String) =
        OfficialDataRepository(store, object : OfficialRemoteSource { override suspend fun fetch() = fetch() }, { "" }, now,
            debugLog = {}, parsingDispatcher = Dispatchers.Unconfined)

    private class Store : OfficialDataStore {
        private var value = OfficialCache(null, null, emptyList())
        override val cache = kotlinx.coroutines.flow.MutableStateFlow(value)
        override suspend fun snapshot() = value
        override suspend fun replace(payload: OfficialPayload, syncedAt: Instant) {
            value = OfficialCache(payload.generatedAt, syncedAt, payload.series); cache.value = value
        }
        override suspend fun touch(syncedAt: Instant) {
            value = value.copy(lastSyncedAt = syncedAt); cache.value = value
        }
    }

    private fun payload(generatedAt: Instant, vararg rows: String) = OfficialDataParser.parse(json(generatedAt, *rows))
    private fun row(id: Int) = """{"tmdbId":$id,"title":"Example","nextSeasonNumber":2,"status":"RENEWED","releaseDate":null,"releaseYear":null,"sourceName":"Official Press","sourceUrl":"https://press.example.com/example","announcementDate":null,"lastChecked":"2026-09-26"}"""
    private fun json(generatedAt: Instant, vararg rows: String) = """{"schemaVersion":1,"generatedAt":"$generatedAt","series":[${rows.joinToString(",")}] }"""

    companion object {
        private val T1 = Instant.parse("2026-09-26T12:00:00Z")
        private val T2 = Instant.parse("2026-09-27T12:00:00Z")
        private val T3 = Instant.parse("2026-09-28T12:00:00Z")
    }
}
