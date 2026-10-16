package com.example.tvseriestracker.data.remote

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkerParameters
import androidx.work.WorkManager
import androidx.work.WorkInfo
import androidx.work.workDataOf
import com.example.tvseriestracker.BuildConfig
import com.example.tvseriestracker.data.DataStoreMonitoringIntentStore
import com.example.tvseriestracker.data.MonitoringObligations
import com.example.tvseriestracker.data.MonitoringSubmission
import com.google.gson.JsonParser
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

enum class MonitoringRequestResult { QUEUED, ALREADY_QUEUED, TEMPORARY_FAILURE, PERMANENT_FAILURE, UNAVAILABLE }

internal fun classifyResponse(status: Int, body: String): MonitoringRequestResult {
    if (status == 202) {
        val state = runCatching { JsonParser.parseString(body).asJsonObject.get("state").asString }.getOrNull()
        return when (state) {
            "queued" -> MonitoringRequestResult.QUEUED
            "already_queued" -> MonitoringRequestResult.ALREADY_QUEUED
            else -> MonitoringRequestResult.TEMPORARY_FAILURE
        }
    }
    return if (status == 408 || status == 429 || status in 500..599)
        MonitoringRequestResult.TEMPORARY_FAILURE else MonitoringRequestResult.PERMANENT_FAILURE
}

internal fun monitoringWorkName(tmdbId: Int) = "official-monitoring-$tmdbId"
internal val monitoringExistingWorkPolicy = ExistingWorkPolicy.KEEP
internal fun monitoringNeedsWork(states: List<WorkInfo.State>) = states.none { !it.isFinished }
/** Invalid optional transport configuration must not prevent local app startup. */
fun deferredMonitoringProtocol(baseUrl: String): MonitoringProtocol = object : MonitoringProtocol {
    private val delegate by lazy { HttpMonitoringProtocol(baseUrl) }
    override suspend fun submit(tmdbId: Int) = delegate.submit(tmdbId)
    override suspend fun status(tmdbId: Int, receipt: String) = delegate.status(tmdbId, receipt)
}
internal fun monitoringWorkRequest(tmdbId: Int) = OneTimeWorkRequestBuilder<MonitoringRequestWorker>()
    .setInputData(workDataOf("tmdbId" to tmdbId))
    .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
    .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
    .build()
internal fun shouldRetry(result: MonitoringRequestResult, attempts: Int) =
    result == MonitoringRequestResult.TEMPORARY_FAILURE && attempts < 5

interface MonitoringRequestQueue {
    fun enqueue(tmdbId: Int)
    suspend fun ensure(tmdbId: Int) { enqueue(tmdbId) }
}

class WorkManagerMonitoringRequestQueue(private val context: Context) : MonitoringRequestQueue {
    override fun enqueue(tmdbId: Int) {
        if (tmdbId <= 0 || BuildConfig.SERIES_REQUEST_API_BASE_URL.isBlank()) return
        WorkManager.getInstance(context).enqueueUniqueWork(monitoringWorkName(tmdbId), monitoringExistingWorkPolicy,
            monitoringWorkRequest(tmdbId))
    }
    override suspend fun ensure(tmdbId: Int) = withContext(Dispatchers.IO) {
        val manager = WorkManager.getInstance(context)
        val existing = manager.getWorkInfosForUniqueWork(monitoringWorkName(tmdbId)).get(10, TimeUnit.SECONDS)
        if (monitoringNeedsWork(existing.map { it.state })) {
            manager.enqueueUniqueWork(monitoringWorkName(tmdbId), monitoringExistingWorkPolicy,
                monitoringWorkRequest(tmdbId)).result.get(10, TimeUnit.SECONDS)
        }
        Unit
    }
}

class MonitoringRequestWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val tmdbId = inputData.getInt("tmdbId", 0)
        if (tmdbId <= 0) return Result.failure()
        return try {
            val obligations = MonitoringObligations(DataStoreMonitoringIntentStore(applicationContext))
            val sent = MonitoringSubmission(obligations,
                HttpMonitoringProtocol(BuildConfig.SERIES_REQUEST_API_BASE_URL)).submit(tmdbId)
            if (sent) Result.success() else if (runAttemptCount < 5) Result.retry() else Result.failure()
        } catch (cancel: CancellationException) { throw cancel }
        catch (_: Exception) { if (runAttemptCount < 5) Result.retry() else Result.failure() }
    }
}
