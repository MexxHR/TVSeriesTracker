package com.example.tvseriestracker.data

import com.example.tvseriestracker.data.remote.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.junit.Assert.*
import org.junit.Test
import java.time.Instant

class MonitoringReconciliationTest {
    class Store : MonitoringIntentStore {
        private val state = MutableStateFlow<Map<Int, MonitoringIntent>>(emptyMap())
        private val mutex = Mutex()
        private var statusAfter = 0L
        private val submissions = mutableListOf<Long>()
        override val changes = state.asStateFlow()
        override suspend fun snapshot() = state.value
        override suspend fun update(transform: (Map<Int, MonitoringIntent>) -> Map<Int, MonitoringIntent>) {
            mutex.withLock { state.value = transform(state.value) }
        }
        override suspend fun statusReadyAt() = statusAfter
        override suspend fun reserveStatusWindow(now: Long) = mutex.withLock {
            if (statusAfter > now) false else { statusAfter = now + 120_000; true }
        }
        override suspend fun reserveSubmissionDelay(now: Long) = mutex.withLock {
            submissions.removeAll { now - it > MonitoringSubmission.SUBMISSION_REPAIR_MILLIS }
            if (submissions.size >= 3) submissions.first() + MonitoringSubmission.SUBMISSION_REPAIR_MILLIS + 1 - now
            else { submissions.add(now); 0L }
        }
    }
    class Protocol : MonitoringProtocol {
        var posts = 0; var checks = 0
        var reply = SubmissionReply(true, "a".repeat(64))
        var status = ReceiptReply(ReceiptState.PENDING)
        var onPost: suspend () -> Unit = {}
        override suspend fun submit(tmdbId: Int): SubmissionReply { posts++; onPost(); return reply }
        override suspend fun status(tmdbId: Int, receipt: String): ReceiptReply { checks++; return status }
    }
    private class Fixture {
        var now = 0L; var fetches = 0; var enqueues = 0; var offline = false
        val store = Store(); val protocol = Protocol(); val obligations = MonitoringObligations(store) { now }
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Unconfined)
        val submission = MonitoringSubmission(obligations, protocol) { now }
        fun reconciler() = MonitoringReconciler(scope, obligations, protocol,
            ensureSubmission = { enqueues++; submission.submit(it); Unit },
            refreshCanonical = { fetches++; if (offline) error("synthetic offline") }, clock = { now })
        suspend fun add(id: Int = 1): MonitoringIntent {
            val item = obligations.prepare(id, "tmdb-$id"); obligations.activate(item)
            return requireNotNull(store.snapshot()[id])
        }
        suspend fun accepted(id: Int = 1) { add(id); submission.submit(id) }
    }

    @Test fun prepareSurvivesDeathBeforeWatchlistActivationAndEnqueue() = runBlocking {
        val f = Fixture(); f.obligations.prepare(1, "tmdb-1")
        assertEquals(MonitoringPhase.PREPARED, f.store.snapshot()[1]?.phase)
        val restarted = MonitoringObligations(f.store) { f.now }
        restarted.repairPrepared { setOf("tmdb-1") }
        f.reconciler().cycle()
        assertEquals(1, f.protocol.posts); assertEquals(MonitoringPhase.RECEIPT, f.store.snapshot()[1]?.phase)
        f.scope.cancel()
    }

    @Test fun uncommittedPreparedIntentIsDiscardedOnRestart() = runBlocking {
        val f = Fixture(); f.obligations.prepare(1, "tmdb-1")
        f.obligations.repairPrepared { emptySet() }
        assertTrue(f.store.snapshot().isEmpty()); f.scope.cancel()
    }

    @Test fun enqueueFailureKeepsDurableSubmissionAndLaterRepair() = runBlocking {
        val f = Fixture(); f.add()
        val broken = MonitoringReconciler(f.scope, f.obligations, f.protocol, { error("synthetic enqueue") }, {}, clock = { f.now })
        broken.cycle(); assertEquals(MonitoringPhase.SUBMIT, f.store.snapshot()[1]?.phase)
        f.now += 120_000; f.reconciler().cycle(); assertEquals(1, f.protocol.posts); f.scope.cancel()
    }

    @Test fun receiptPersistenceStopsPostsAndPendingSurvivesManyCycles() = runBlocking {
        val f = Fixture(); f.accepted()
        repeat(5) { f.now += 120_000; f.reconciler().cycle() }
        assertEquals(1, f.protocol.posts); assertEquals(5, f.protocol.checks)
        assertEquals(MonitoringPhase.RECEIPT, f.store.snapshot()[1]?.phase); f.scope.cancel()
    }

    @Test fun completePersistsBeforeCanonicalAndRestartReplaysFetch() = runBlocking {
        val f = Fixture(); f.accepted(); f.protocol.status = ReceiptReply(ReceiptState.COMPLETE)
        f.now = 120_000; f.reconciler().cycle()
        assertEquals(MonitoringPhase.CANONICAL, f.store.snapshot()[1]?.phase); assertEquals(0, f.fetches)
        f.now += 30_000; f.reconciler().cycle()
        assertTrue(f.store.snapshot().isEmpty()); assertEquals(1, f.fetches); assertEquals(1, f.protocol.posts); f.scope.cancel()
    }

    @Test fun actualRepositorySameNewerAndNoFactCompleteWithoutTimestampHeuristics() = runBlocking {
        for (existing in listOf(false, true)) for (newer in listOf(false, true)) {
            val f = Fixture(); f.accepted(); f.protocol.status = ReceiptReply(ReceiptState.COMPLETE)
            val initial = "2026-09-26T12:00:00Z"
            val remote = if (newer) "2026-09-27T12:00:00Z" else initial
            val row = if (existing) """{"tmdbId":1,"title":"Synthetic","nextSeasonNumber":2,"status":"RENEWED","releaseDate":null,"releaseYear":null,"sourceName":"Official Press","sourceUrl":"https://press.example.invalid/synthetic","announcementDate":null,"lastChecked":"2026-09-26"}""" else ""
            fun json(time: String) = """{"schemaVersion":1,"generatedAt":"$time","series":[$row]}"""
            val cache = object : OfficialDataStore {
                override val cache = MutableStateFlow(OfficialCache(null, null, emptyList()))
                override suspend fun snapshot() = cache.value
                override suspend fun replace(payload: OfficialPayload, syncedAt: Instant) {
                    cache.value = OfficialCache(payload.generatedAt, syncedAt, payload.series)
                }
                override suspend fun touch(syncedAt: Instant) { cache.value = cache.value.copy(lastSyncedAt = syncedAt) }
            }
            val official = OfficialDataRepository(cache, object: OfficialRemoteSource {
                override suspend fun fetch() = json(remote)
            }, { json(initial) })
            official.bootstrap()
            val reconciler = MonitoringReconciler(f.scope, f.obligations, f.protocol, {},
                refreshCanonical = { official.refresh("phase2-test") }, clock = { f.now })
            f.now = 120_000; reconciler.cycle(); assertNotNull(f.store.snapshot()[1])
            f.now += 30_000; reconciler.cycle()
            assertTrue(f.store.snapshot().isEmpty())
            assertEquals(Instant.parse(remote), cache.snapshot().generatedAt)
            assertEquals(if (existing) 1 else 0, cache.snapshot().series.size)
            f.scope.cancel()
        }
    }

    @Test fun canonicalFailureRetainsFetchIntentWithoutAnotherPostOrStatus() = runBlocking {
        val f = Fixture(); f.accepted(); f.protocol.status = ReceiptReply(ReceiptState.COMPLETE)
        f.now = 120_000; f.reconciler().cycle(); f.now += 30_000; f.offline = true; f.reconciler().cycle()
        assertEquals(MonitoringPhase.CANONICAL, f.store.snapshot()[1]?.phase)
        f.offline = false; f.now += 120_000; f.reconciler().cycle()
        assertTrue(f.store.snapshot().isEmpty()); assertEquals(1, f.protocol.posts); assertEquals(1, f.protocol.checks); f.scope.cancel()
    }

    @Test fun unknownRecoversOnlyAfterDurableCooldown() = runBlocking {
        val f = Fixture(); f.accepted(); f.protocol.status = ReceiptReply(ReceiptState.UNKNOWN)
        f.now = 120_000; f.reconciler().cycle()
        assertEquals(MonitoringPhase.SUBMIT, f.store.snapshot()[1]?.phase)
        f.reconciler().cycle(); assertEquals(1, f.protocol.posts)
        f.now += MonitoringSubmission.SUBMISSION_REPAIR_MILLIS; f.reconciler().cycle()
        assertEquals(2, f.protocol.posts); f.scope.cancel()
    }

    @Test fun status429And5xxRetainReceiptAndRespectRetryAfter() = runBlocking {
        val f = Fixture(); f.accepted()
        f.protocol.status = ReceiptReply(ReceiptState.TEMPORARY_FAILURE, 3600_000)
        f.now = 120_000; f.reconciler().cycle()
        f.now += 120_000; f.reconciler().cycle()
        assertEquals(1, f.protocol.checks); assertEquals(1, f.protocol.posts)
        assertEquals("a".repeat(64), f.store.snapshot()[1]?.receipt); f.scope.cancel()
    }

    @Test fun multipleCompleteReceiptsShareOneCanonicalFetch() = runBlocking {
        val f = Fixture(); f.accepted(1); f.accepted(2); f.protocol.status = ReceiptReply(ReceiptState.COMPLETE)
        f.now = 120_000; f.reconciler().cycle(); f.now += 30_000; f.reconciler().cycle()
        assertEquals(1, f.fetches); assertTrue(f.store.snapshot().isEmpty()); f.scope.cancel()
    }

    @Test fun durableGlobalStatusBudgetSurvivesCoordinatorRestartAndBacklog() = runBlocking {
        val f = Fixture(); for (id in 1..8) {
            if (id > 1 && (id-1) % 3 == 0) f.now += MonitoringSubmission.SUBMISSION_REPAIR_MILLIS + 1
            f.accepted(id)
        }
        f.now += 120_000
        repeat(8) { f.reconciler().cycle() }
        assertEquals(2, f.protocol.checks)
        f.now += 120_000; f.reconciler().cycle(); assertEquals(4, f.protocol.checks); f.scope.cancel()
    }

    @Test fun removeReaddRejectsOldInflightReceiptAndOldClear() = runBlocking {
        val f = Fixture(); val first = f.add()
        f.protocol.onPost = { f.obligations.removeSeries("tmdb-1"); f.add() }
        f.submission.submit(1)
        val second = requireNotNull(f.store.snapshot()[1])
        assertNotEquals(first.localKey, second.localKey); assertEquals(MonitoringPhase.SUBMIT, second.phase)
        f.obligations.change(first) { null }; assertNotNull(f.store.snapshot()[1]); f.scope.cancel()
    }

    @Test fun postAcceptedButReceiptSaveCrashConvergesWithBoundedDuplicate() = runBlocking {
        val f = Fixture(); f.add()
        f.protocol.onPost = { throw CancellationException("synthetic process stop after server acceptance") }
        try { f.submission.submit(1) } catch (_: CancellationException) { }
        assertEquals(MonitoringPhase.SUBMIT, f.store.snapshot()[1]?.phase)
        f.protocol.onPost = {}; f.submission.submit(1); assertEquals(1, f.protocol.posts)
        f.now += MonitoringSubmission.SUBMISSION_REPAIR_MILLIS; f.submission.submit(1)
        assertEquals(2, f.protocol.posts); assertNotNull(f.store.snapshot()[1]?.receipt); f.scope.cancel()
    }

    @Test fun legacyNoReceiptGetsOnlyThreeRefreshesAndNeverBatchCompletion() = runBlocking {
        val f = Fixture(); f.protocol.reply = SubmissionReply(true)
        f.accepted(); val reconciler = f.reconciler()
        for (time in listOf(30_000L, 90_000L, 210_000L, 600_000L)) { f.now = time; reconciler.cycle() }
        assertEquals(3, f.fetches); assertEquals(0, f.protocol.checks); assertEquals(1, f.protocol.posts)
        assertEquals(MonitoringPhase.LEGACY, f.store.snapshot()[1]?.phase)
        assertEquals(210_000L + 24 * 60 * 60_000L, f.store.snapshot()[1]?.nextAt); f.scope.cancel()
    }

    @Test fun duplicateAddDoesNotReplaceReceiptOrResetCooldown() = runBlocking {
        val f = Fixture(); f.accepted(); val before = f.store.snapshot()[1]
        val existing = f.obligations.prepare(1, "tmdb-1"); f.obligations.activate(existing)
        assertEquals(before, f.store.snapshot()[1]); f.scope.cancel()
    }

    @Test fun submissionBacklogIsDurablyBoundedAndLegacyCanDiscoverLaterCutoverOnResume() = runBlocking {
        val f = Fixture()
        for (id in 1..8) { f.add(id); f.submission.submit(id) }
        assertEquals(3, f.protocol.posts)
        repeat(10) { f.reconciler().cycle() }; assertEquals(3, f.protocol.posts)
        f.scope.cancel()
        val legacy = Fixture(); legacy.protocol.reply = SubmissionReply(true); legacy.accepted()
        for (time in listOf(30_000L,90_000L,210_000L)) { legacy.now=time; legacy.reconciler().cycle() }
        legacy.now += 24*60*60_000L
        legacy.obligations.repairPrepared { setOf("tmdb-1") }
        legacy.protocol.reply = SubmissionReply(true, "c".repeat(64)); legacy.reconciler().cycle()
        assertEquals(MonitoringPhase.RECEIPT, legacy.store.snapshot()[1]?.phase); legacy.scope.cancel()
    }

    @Test fun equalClockConcurrentTransportReservationsDispatchOnlyOnce() = runBlocking {
        val base = Store(); val normal = MonitoringObligations(base) { 0 }
        normal.activate(normal.prepare(1, "tmdb-1"))
        val barrier = CompletableDeferred<Unit>(); var reads = 0
        val racing = object : MonitoringIntentStore by base {
            override suspend fun snapshot(): Map<Int, MonitoringIntent> {
                val captured = base.snapshot()
                if (++reads <= 2) { if (reads == 2) barrier.complete(Unit); barrier.await() }
                return captured
            }
        }
        val protocol = Protocol()
        val submission = MonitoringSubmission(MonitoringObligations(racing) { 0 }, protocol) { 0 }
        coroutineScope { listOf(async { submission.submit(1) }, async { submission.submit(1) }).awaitAll() }
        assertEquals(1, protocol.posts)
    }

    @Test fun removeReaddDuringCanonicalFetchCannotClearNewObligation() = runBlocking {
        val f = Fixture(); f.accepted(); f.protocol.status = ReceiptReply(ReceiptState.COMPLETE)
        f.now = 120_000; f.reconciler().cycle(); val old = requireNotNull(f.store.snapshot()[1])
        f.now += 30_000
        val reconciler = MonitoringReconciler(f.scope, f.obligations, f.protocol, {}, {
            f.obligations.removeSeries("tmdb-1"); f.add()
        }, clock = { f.now })
        reconciler.cycle()
        assertNotEquals(old.localKey, f.store.snapshot()[1]?.localKey)
        assertEquals(MonitoringPhase.SUBMIT, f.store.snapshot()[1]?.phase); f.scope.cancel()
    }

    @Test fun deathAfterSuccessfulCanonicalFetchBeforeClearReplaysOnlyFetch() = runBlocking {
        val f = Fixture(); f.accepted(); f.protocol.status = ReceiptReply(ReceiptState.COMPLETE)
        f.now = 120_000; f.reconciler().cycle(); f.now += 30_000
        var kill = true
        val disk = object: MonitoringIntentStore by f.store {
            override suspend fun update(transform: (Map<Int, MonitoringIntent>) -> Map<Int, MonitoringIntent>) {
                f.store.update { old ->
                    val changed = transform(old)
                    if (kill && changed.isEmpty()) { kill = false; throw CancellationException("synthetic death before durable clear") }
                    changed
                }
            }
        }
        val restarted = MonitoringObligations(disk) { f.now }
        fun coordinator() = MonitoringReconciler(f.scope, restarted, f.protocol, {}, { f.fetches++ }, clock = { f.now })
        try { coordinator().cycle() } catch (_: CancellationException) { }
        assertEquals(MonitoringPhase.CANONICAL, f.store.snapshot()[1]?.phase)
        coordinator().cycle()
        assertTrue(f.store.snapshot().isEmpty()); assertEquals(2, f.fetches); assertEquals(1, f.protocol.posts); f.scope.cancel()
    }

    @Test fun addWriteAheadSurvivesQueueFailureWithoutRollingBackWatchlist() = runBlocking {
        val f = Fixture(); var saved = false
        val repository = object : SeriesRepository {
            override val tracked: Flow<List<Series>> = flowOf(emptyList())
            override fun observeSeries(id: String): Flow<Series?> = flowOf(null)
            override suspend fun search(query: String, language: AppLanguage): List<Series> = emptyList()
            override suspend fun track(series: Series, language: AppLanguage) {
                assertEquals(MonitoringPhase.PREPARED, f.store.snapshot()[1]?.phase); saved = true
            }
            override suspend fun untrack(id: String) = Unit
            override suspend fun refreshMetadata(id: String, language: AppLanguage) = Unit
        }
        trackAndQueueMonitoring(repository, object: MonitoringRequestQueue {
            override fun enqueue(tmdbId: Int) { error("synthetic enqueue unavailable") }
        }, Series("tmdb-1", "Synthetic", platform="", tmdbId=1), AppLanguage.HR, f.obligations)
        assertTrue(saved); assertEquals(MonitoringPhase.SUBMIT, f.store.snapshot()[1]?.phase); f.scope.cancel()
    }

    @Test fun backgroundCancelsCadenceWhileDurableStateRemainsAndResumeRepairs() = runBlocking {
        val f = Fixture(); f.add(); val reconciler = f.reconciler()
        reconciler.setActive(true); yield(); reconciler.setActive(false)
        val count = f.protocol.posts; delay(30); assertEquals(count, f.protocol.posts)
        assertNotNull(f.store.snapshot()[1]); reconciler.setActive(true); yield(); reconciler.setActive(false)
        assertEquals(1, f.protocol.posts); f.scope.cancel()
    }

    @Test fun codecValidatesEveryFieldAndRedactsReceipt() {
        val item = MonitoringIntent(1, "tmdb-1", "b".repeat(32), MonitoringPhase.RECEIPT, "a".repeat(64), 120_000)
        assertEquals(item, MonitoringIntentCodec.decode(MonitoringIntentCodec.encode(item)))
        for (text in listOf("", "garbage", "1|1|x|bad|CANONICAL|-|0|0", "x".repeat(3000))) assertNull(MonitoringIntentCodec.decode(text))
        assertFalse(item.toString().contains("a".repeat(64)))
    }
}
