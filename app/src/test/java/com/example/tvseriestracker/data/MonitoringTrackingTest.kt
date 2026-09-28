package com.example.tvseriestracker.data

import com.example.tvseriestracker.data.remote.MonitoringRequestQueue
import com.example.tvseriestracker.data.remote.MonitoringRequestResult
import com.example.tvseriestracker.data.remote.classifyResponse
import com.example.tvseriestracker.data.remote.monitoringWorkName
import com.example.tvseriestracker.data.remote.shouldRetry
import com.example.tvseriestracker.data.remote.monitoringWorkRequest
import com.example.tvseriestracker.data.remote.monitoringExistingWorkPolicy
import androidx.work.BackoffPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class MonitoringTrackingTest {
    private class FakeSeriesRepository : SeriesRepository {
        var added = false
        override val tracked: Flow<List<Series>> = flowOf(emptyList())
        override fun observeSeries(id: String): Flow<Series?> = flowOf(null)
        override suspend fun search(query: String, language: AppLanguage): List<Series> = emptyList()
        override suspend fun track(series: Series, language: AppLanguage) { added = true }
        override suspend fun untrack(id: String) = Unit
        override suspend fun refreshMetadata(id: String, language: AppLanguage) = Unit
    }

    @Test fun localAddSurvivesQueueFailure() = runBlocking {
        val repository = FakeSeriesRepository()
        trackAndQueueMonitoring(repository, object : MonitoringRequestQueue {
            override fun enqueue(tmdbId: Int) { throw IllegalStateException("offline") }
        }, Series("tmdb-1", "Test", platform = "", tmdbId = 1), AppLanguage.HR)
        assertTrue(repository.added)
    }

    @Test fun successfulLocalAddQueuesOnlyId() = runBlocking {
        val repository = FakeSeriesRepository()
        val ids = mutableListOf<Int>()
        trackAndQueueMonitoring(repository, object : MonitoringRequestQueue {
            override fun enqueue(tmdbId: Int) { ids.add(tmdbId) }
        }, Series("tmdb-123", "Test", platform = "", tmdbId = 123), AppLanguage.HR)
        assertTrue(repository.added)
        assertEquals(listOf(123), ids)
    }

    @Test fun queuedAndDuplicateTransportResponsesStayTransportStates() {
        assertEquals(MonitoringRequestResult.QUEUED, classifyResponse(202, "{\"state\":\"queued\"}"))
        assertEquals(MonitoringRequestResult.ALREADY_QUEUED, classifyResponse(202, "{ \"state\" : \"already_queued\" }"))
    }

    @Test fun retryClassificationIsBounded() {
        assertTrue(shouldRetry(classifyResponse(429, ""), 0))
        assertTrue(shouldRetry(classifyResponse(503, ""), 4))
        assertFalse(shouldRetry(classifyResponse(503, ""), 5))
        assertFalse(shouldRetry(classifyResponse(400, ""), 0))
        assertFalse(shouldRetry(MonitoringRequestResult.UNAVAILABLE, 0))
    }

    @Test fun uniqueWorkKeyUsesTmdbIdentity() {
        assertEquals("official-monitoring-123", monitoringWorkName(123))
        assertEquals(monitoringWorkName(123), monitoringWorkName(123))
        assertEquals(ExistingWorkPolicy.KEEP, monitoringExistingWorkPolicy)
    }

    @Test fun workWaitsForNetworkAndUsesExponentialBackoff() {
        val work = monitoringWorkRequest(123)
        assertEquals(123, work.workSpec.input.getInt("tmdbId", 0))
        assertEquals(NetworkType.CONNECTED, work.workSpec.constraints.requiredNetworkType)
        assertEquals(BackoffPolicy.EXPONENTIAL, work.workSpec.backoffPolicy)
    }
}
