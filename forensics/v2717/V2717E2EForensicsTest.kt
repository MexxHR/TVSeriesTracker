package com.example.tvseriestracker.data

import java.time.Instant
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import org.junit.Assert.*
import org.junit.Test

/** CURRENT_BEHAVIOR_TEST; synthetic payloads, actual frozen coordinator/repository. */
class V2717E2EForensicsTest {
    private val t0 = Instant.parse("2026-10-15T00:00:00Z")
    private val id = 88324
    private fun row(id: Int) = """{"tmdbId":$id,"title":"Synthetic forensic identity","nextSeasonNumber":7,"status":"RENEWED","releaseDate":null,"releaseYear":null,"sourceName":"Synthetic fixture","sourceUrl":"https://example.com/fixture","announcementDate":null,"lastChecked":"2026-10-15"}"""
    private fun json(newer: Boolean, vararg ids: Int) = """{"schemaVersion":1,"generatedAt":"${t0.plusSeconds(if(newer) 1 else 0)}","series":[${ids.joinToString(",") { row(it) }}]}"""
    private class Store : OfficialDataStore {
        override val cache = MutableStateFlow(OfficialCache(null,null,emptyList()))
        override suspend fun snapshot() = cache.value
        override suspend fun replace(payload: OfficialPayload,syncedAt:Instant) { cache.value=OfficialCache(payload.generatedAt,syncedAt,payload.series) }
        override suspend fun touch(syncedAt:Instant) { cache.value=cache.value.copy(lastSyncedAt=syncedAt) }
    }
    private fun repo(store: Store, now:()->Instant={t0.plusSeconds(240)}, fetch:suspend()->String) = OfficialDataRepository(store,
        object:OfficialRemoteSource { override suspend fun fetch()=fetch() },{""},now,debugLog={},parsingDispatcher=Dispatchers.Unconfined)
    private suspend fun appearingAt(seconds:Long):Pair<List<Long>,Boolean> {
        var time=0L;val attempts=mutableListOf<Long>();val store=Store()
        store.replace(OfficialDataParser.parse(json(false)),t0)
        val repository=repo(store) { attempts+=time;json(time>=seconds, *if(time>=seconds) intArrayOf(id) else intArrayOf()) }
        val scope=CoroutineScope(SupervisorJob()+Dispatchers.Unconfined)
        val poll=RequestedOfficialRefresh(scope,repository,wait={time+=it/1000})
        poll.setActive(true);poll.requested(id);scope.cancel()
        return attempts to store.snapshot().series.any { it.tmdbId==id }
    }
    @Test fun scenario01_factBeforeFirstPoll()=runBlocking { assertEquals(listOf(30L) to true,appearingAt(20)) }
    @Test fun scenario02_factBetween30And60()=runBlocking { assertEquals(listOf(30L,90L) to true,appearingAt(45)) }
    @Test fun scenario03_factBetween60And120()=runBlocking { assertEquals(listOf(30L,90L,210L) to true,appearingAt(100)) }
    @Test fun scenario04_factAfter120Before210()=runBlocking { assertEquals(listOf(30L,90L,210L) to true,appearingAt(150)) }
    @Test fun scenario05_tabChangeDoesNotChangeRootLifecycle()=runBlocking { assertEquals(listOf(30L,90L) to true,appearingAt(70)) }
    @Test fun scenario06_backgroundDropsPendingBeforeFetch()=runBlocking {
        val store=Store();var calls=0;val gate=CompletableDeferred<Unit>();val scope=CoroutineScope(SupervisorJob()+Dispatchers.Unconfined)
        val repository=repo(store) { calls++;json(true,id) }
        val poll=RequestedOfficialRefresh(scope,repository,wait={gate.await()})
        poll.setActive(true);poll.requested(id);poll.setActive(false);poll.setActive(true);gate.complete(Unit)
        assertEquals(0,calls);assertTrue(store.snapshot().series.isEmpty());scope.cancel()
    }
    @Test fun scenario07_restartLosesPendingWhileCacheIsFresh()=runBlocking {
        val store=Store();store.replace(OfficialDataParser.parse(json(false)),t0);var calls=0
        val repository=repo(store) { calls++;json(true,id) }
        val oldScope=CoroutineScope(SupervisorJob()+Dispatchers.Unconfined)
        val old=RequestedOfficialRefresh(oldScope,repository,wait={awaitCancellation()});old.setActive(true);old.requested(id);oldScope.cancel()
        val newScope=CoroutineScope(SupervisorJob()+Dispatchers.Unconfined)
        val restarted=RequestedOfficialRefresh(newScope,repository,wait={});restarted.setActive(true)
        repository.refreshIfStale();assertEquals(0,calls);assertTrue(store.snapshot().series.isEmpty())
        repository.refresh("manual");assertEquals(1,calls);assertEquals(id,store.snapshot().series.single().tmdbId);newScope.cancel()
    }
    @Test fun scenario08_freshStartupDoesNotFetchNewServerFact()=runBlocking {
        val store=Store();store.replace(OfficialDataParser.parse(json(false)),t0);var calls=0
        repo(store) { calls++;json(true,id) }.refreshIfStale();assertEquals(0,calls)
    }
    @Test fun scenario09_manualAndAutomaticCoalesceActualRepositoryFetch()=runBlocking {
        val store=Store();var calls=0;val response=CompletableDeferred<String>();val waiting=CompletableDeferred<Unit>()
        val repository=repo(store) { calls++;waiting.complete(Unit);response.await() }
        val scope=CoroutineScope(SupervisorJob()+Dispatchers.Unconfined)
        val poll=RequestedOfficialRefresh(scope,repository,wait={});poll.setActive(true);poll.requested(id);waiting.await()
        val manual=async { repository.refresh("manual") };yield();response.complete(json(true,id));assertTrue(manual.await())
        assertEquals(1,calls);assertEquals(id,store.snapshot().series.single().tmdbId);scope.cancel()
    }
    @Test fun scenario10_twoIdsShareOneBatch()=runBlocking {
        val store=Store();var calls=0;val gate=CompletableDeferred<Unit>();val scope=CoroutineScope(SupervisorJob()+Dispatchers.Unconfined)
        val poll=RequestedOfficialRefresh(scope,repo(store){calls++;json(true,id,90296)},wait={gate.await()})
        poll.setActive(true);poll.requested(id);poll.requested(90296);gate.complete(Unit)
        assertEquals(1,calls);assertEquals(setOf(id,90296),store.snapshot().series.map{it.tmdbId}.toSet());scope.cancel()
    }
    @Test fun scenario11_duplicateAcceptedIdDoesNotResetBatch()=runBlocking {
        val store=Store();var calls=0;val gate=CompletableDeferred<Unit>();var waits=0;val scope=CoroutineScope(SupervisorJob()+Dispatchers.Unconfined)
        val poll=RequestedOfficialRefresh(scope,repo(store){calls++;json(false)},wait={waits++;if(waits==1)gate.await()})
        poll.setActive(true);poll.requested(id);poll.requested(id);gate.complete(Unit)
        assertEquals(3,calls);assertEquals(3,waits);scope.cancel()
    }
    @Test fun scenario14_sameTimestampWithNewRowIsRejectedThenManualSameStillRejected()=runBlocking {
        val store=Store();store.replace(OfficialDataParser.parse(json(false)),t0)
        val repository=repo(store){json(false,id)}
        assertFalse(repository.refresh("requested-follow-up"));assertTrue(store.snapshot().series.isEmpty())
        assertFalse(repository.refresh("manual"));assertTrue(store.snapshot().series.isEmpty())
    }
    @Test fun scenario15_newerTimestampReachesObservableCache()=runBlocking {
        val store=Store();store.replace(OfficialDataParser.parse(json(false)),t0)
        assertTrue(repo(store){json(true,id)}.refresh("requested-follow-up"))
        assertEquals(SeriesStatus.RENEWED,store.cache.value.series.single().status)
    }
    @Test fun virginRiverAfterLastPollStaleAutoThenManualSucceeds()=runBlocking {
        var time=0L;val store=Store();store.replace(OfficialDataParser.parse(json(false)),t0)
        val repository=repo(store){json(time>=220,*if(time>=220)intArrayOf(id)else intArrayOf())}
        val scope=CoroutineScope(SupervisorJob()+Dispatchers.Unconfined)
        val poll=RequestedOfficialRefresh(scope,repository,wait={time+=it/1000});poll.setActive(true);poll.requested(id)
        assertEquals(210L,time);assertTrue(store.snapshot().series.isEmpty())
        time=240;assertTrue(repository.refresh("manual"));assertEquals(id,store.snapshot().series.single().tmdbId);scope.cancel()
    }
    @Test fun existingOlderFactForSameIdPreventsAnyFollowup()=runBlocking {
        val store=Store();store.replace(OfficialDataParser.parse(json(false,id)),t0);var calls=0
        val scope=CoroutineScope(SupervisorJob()+Dispatchers.Unconfined)
        val poll=RequestedOfficialRefresh(scope,repo(store){calls++;json(true,id)},wait={})
        poll.setActive(true);poll.requested(id);assertEquals(0,calls);scope.cancel()
    }
}
