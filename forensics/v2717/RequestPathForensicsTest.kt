package com.example.tvseriestracker.data
import com.example.tvseriestracker.data.remote.MonitoringRequestQueue
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
/** CURRENT_BEHAVIOR_TEST of actual production trackAndQueueMonitoring.
 * ID 123 is SYNTHETIC, never claimed as Black Mirror's unconfirmed TMDB ID.
 * DAO/WorkManager/device state are outside this contract-level reproduction.
 */
class RequestPathForensicsTest {
    @Test fun localAddCanSucceedWhileEnqueueFailsSilently()=runBlocking {
        var inserted=false
        val repository=object:SeriesRepository { override suspend fun track(series:Series,language:AppLanguage) { inserted=true } }
        val queue=object:MonitoringRequestQueue { override fun enqueue(tmdbId:Int) { throw IllegalStateException("synthetic queue unavailable") } }
        trackAndQueueMonitoring(repository,queue,Series("synthetic-123","Synthetic identity",platform="",tmdbId=123),AppLanguage.EN)
        assertTrue(inserted)
    }
    @Test fun repeatedWrapperCallDoesNotContainItsOwnDedup()=runBlocking {
        val calls=mutableListOf<Int>();val repository=object:SeriesRepository { override suspend fun track(series:Series,language:AppLanguage) {} }
        val queue=object:MonitoringRequestQueue { override fun enqueue(tmdbId:Int) { calls+=tmdbId } }
        repeat(2) { trackAndQueueMonitoring(repository,queue,Series("synthetic-123","Synthetic identity",platform="",tmdbId=123),AppLanguage.EN) }
        assertEquals(listOf(123,123),calls)
    }
    @Test fun statusHintDoesNotSuppressWrapperSubmission()=runBlocking {
        val calls=mutableListOf<Int>();val repository=object:SeriesRepository { override suspend fun track(series:Series,language:AppLanguage) {} }
        val queue=object:MonitoringRequestQueue { override fun enqueue(tmdbId:Int) { calls+=tmdbId } }
        trackAndQueueMonitoring(repository,queue,Series("synthetic-123","Synthetic identity",platform="",tmdbId=123,status=SeriesStatus.RENEWED),AppLanguage.EN)
        assertEquals(listOf(123),calls)
    }
}
