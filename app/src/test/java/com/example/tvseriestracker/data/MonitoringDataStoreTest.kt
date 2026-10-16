package com.example.tvseriestracker.data

import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import java.nio.file.Files
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.first
import org.junit.Assert.*
import org.junit.Test

class MonitoringDataStoreTest {
    @Test fun actualPreferencesFileSurvivesCloseReopenAndKeepsSettingsSeparate() = runBlocking {
        val folder = Files.createTempDirectory("monitoring-store-test").toFile()
        val file = folder.resolve("monitoring.preferences_pb")
        val settings = folder.resolve("settings.preferences_pb").apply { writeText("existing HR LIGHT watchlist unaffected") }
        val untouched = settings.readBytes()
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        val disk = PreferenceDataStoreFactory.create(scope = scope) { file }
        val store = DataStoreMonitoringIntentStore(disk); val obligations = MonitoringObligations(store) { 1000 }
        val intent = obligations.prepare(1, "tmdb-1"); obligations.activate(intent)
        store.reserveStatusWindow(1000)
        scope.coroutineContext[Job]!!.cancelAndJoin()
        val scope2 = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        try {
            val reopened = DataStoreMonitoringIntentStore(PreferenceDataStoreFactory.create(scope = scope2) { file })
            assertEquals(MonitoringPhase.SUBMIT, reopened.snapshot()[1]?.phase)
            assertFalse(reopened.reserveStatusWindow(1001))
            assertArrayEquals(untouched, settings.readBytes())
        } finally { scope2.coroutineContext[Job]!!.cancelAndJoin(); folder.deleteRecursively() }
    }

    @Test fun receiptCanonicalAndClearPersistThroughRealFileRestarts() = runBlocking {
        val folder = Files.createTempDirectory("monitoring-transitions").toFile()
        val file = folder.resolve("pending.preferences_pb")
        var expected = MonitoringIntent(1, "tmdb-1", "b".repeat(32))
        try {
            for (phase in listOf(MonitoringPhase.PREPARED, MonitoringPhase.SUBMIT, MonitoringPhase.RECEIPT, MonitoringPhase.CANONICAL)) {
                val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
                val store = DataStoreMonitoringIntentStore(PreferenceDataStoreFactory.create(scope = scope) { file })
                if (phase != MonitoringPhase.PREPARED) assertEquals(expected, store.snapshot()[1])
                expected = expected.copy(phase = phase, receipt = if (phase in listOf(MonitoringPhase.RECEIPT, MonitoringPhase.CANONICAL)) "a".repeat(64) else null)
                store.update { mapOf(1 to expected) }
                scope.coroutineContext[Job]!!.cancelAndJoin()
            }
            val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
            val store = DataStoreMonitoringIntentStore(PreferenceDataStoreFactory.create(scope = scope) { file })
            assertEquals(expected, store.snapshot()[1]); store.update { emptyMap() }
            scope.coroutineContext[Job]!!.cancelAndJoin()
            val lastScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
            val last = DataStoreMonitoringIntentStore(PreferenceDataStoreFactory.create(scope = lastScope) { file })
            assertTrue(last.snapshot().isEmpty()); lastScope.coroutineContext[Job]!!.cancelAndJoin()
        } finally { folder.deleteRecursively() }
    }

    @Test fun concurrentActualDataStoreEditsPreserveMultipleIntents() = runBlocking {
        val folder = Files.createTempDirectory("monitoring-concurrent").toFile()
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        try {
            val store = DataStoreMonitoringIntentStore(PreferenceDataStoreFactory.create(scope = scope) { folder.resolve("pending.preferences_pb") })
            val obligations = MonitoringObligations(store)
            coroutineScope { (1..10).map { id -> async(Dispatchers.Default) { obligations.prepare(id, "tmdb-$id") } }.awaitAll() }
            assertEquals((1..10).toSet(), store.snapshot().keys)
            obligations.removeSeries("tmdb-1"); assertFalse(store.snapshot().containsKey(1))
        } finally { scope.coroutineContext[Job]!!.cancelAndJoin(); folder.deleteRecursively() }
    }

    @Test fun malformedRecordRecoversToPreparedInsteadOfFalseComplete() = runBlocking {
        val folder = Files.createTempDirectory("monitoring-corrupt").toFile()
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        try {
            val disk = PreferenceDataStoreFactory.create(scope = scope) { folder.resolve("pending.preferences_pb") }
            disk.edit { it[stringPreferencesKey("intent_1_dG1kYi0x")] = "not-a-valid-record" }
            val store = DataStoreMonitoringIntentStore(disk)
            assertEquals(MonitoringPhase.PREPARED, store.snapshot()[1]?.phase)
            MonitoringObligations(store).repairPrepared { setOf("tmdb-1") }
            assertEquals(MonitoringPhase.SUBMIT, store.snapshot()[1]?.phase)
            disk.edit { it[stringPreferencesKey("intent_invalid_identity")] = "malformed" }
            var rejected = false
            try { store.snapshot() } catch (_: java.io.IOException) { rejected = true }
            assertTrue(rejected)
            assertTrue(disk.data.first().asMap().keys.any { it.name == "intent_invalid_identity" })
        } finally { scope.coroutineContext[Job]!!.cancelAndJoin(); folder.deleteRecursively() }
    }
}
