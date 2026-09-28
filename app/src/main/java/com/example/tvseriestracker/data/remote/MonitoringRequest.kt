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
import androidx.work.workDataOf
import com.example.tvseriestracker.BuildConfig
import com.google.gson.JsonParser
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

enum class MonitoringRequestResult { QUEUED, ALREADY_QUEUED, TEMPORARY_FAILURE, PERMANENT_FAILURE, UNAVAILABLE }

interface SeriesMonitoringRequestService {
    suspend fun requestMonitoring(tmdbId: Int): MonitoringRequestResult
}

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
internal fun monitoringWorkRequest(tmdbId: Int) = OneTimeWorkRequestBuilder<MonitoringRequestWorker>()
    .setInputData(workDataOf("tmdbId" to tmdbId))
    .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
    .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
    .build()
internal fun shouldRetry(result: MonitoringRequestResult, attempts: Int) =
    result == MonitoringRequestResult.TEMPORARY_FAILURE && attempts < 5

class HttpSeriesMonitoringRequestService(
    private val baseUrl: String = BuildConfig.SERIES_REQUEST_API_BASE_URL
) : SeriesMonitoringRequestService {
    override suspend fun requestMonitoring(tmdbId: Int): MonitoringRequestResult = withContext(Dispatchers.IO) {
        if (tmdbId <= 0) return@withContext MonitoringRequestResult.PERMANENT_FAILURE
        if (baseUrl.isBlank()) return@withContext MonitoringRequestResult.UNAVAILABLE
        val base = runCatching { URL(baseUrl.trimEnd('/') + "/") }.getOrNull()
            ?: return@withContext MonitoringRequestResult.UNAVAILABLE
        if (base.protocol != "https") return@withContext MonitoringRequestResult.UNAVAILABLE
        try {
            val connection = URL(base, "v1/series-requests").openConnection() as HttpURLConnection
            try {
                connection.requestMethod = "POST"
                connection.connectTimeout = 8000
                connection.readTimeout = 8000
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                val payload = "{\"tmdbId\":$tmdbId}".toByteArray(Charsets.UTF_8)
                connection.setFixedLengthStreamingMode(payload.size)
                connection.outputStream.use { it.write(payload) }
                val status = connection.responseCode
                val response = if (status == 202) connection.inputStream.bufferedReader().use { it.readText() } else ""
                classifyResponse(status, response)
            } finally {
                connection.disconnect()
            }
        } catch (exception: CancellationException) {
            throw exception
        } catch (_: Exception) {
            MonitoringRequestResult.TEMPORARY_FAILURE
        }
    }
}

interface MonitoringRequestQueue {
    fun enqueue(tmdbId: Int)
}

class WorkManagerMonitoringRequestQueue(private val context: Context) : MonitoringRequestQueue {
    override fun enqueue(tmdbId: Int) {
        if (tmdbId <= 0 || BuildConfig.SERIES_REQUEST_API_BASE_URL.isBlank()) return
        WorkManager.getInstance(context).enqueueUniqueWork(monitoringWorkName(tmdbId), monitoringExistingWorkPolicy,
            monitoringWorkRequest(tmdbId))
    }
}

class MonitoringRequestWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val tmdbId = inputData.getInt("tmdbId", 0)
        if (tmdbId <= 0) return Result.failure()
        return when (HttpSeriesMonitoringRequestService().requestMonitoring(tmdbId)) {
            MonitoringRequestResult.QUEUED, MonitoringRequestResult.ALREADY_QUEUED -> Result.success()
            MonitoringRequestResult.TEMPORARY_FAILURE -> if (shouldRetry(MonitoringRequestResult.TEMPORARY_FAILURE, runAttemptCount)) Result.retry() else Result.failure()
            MonitoringRequestResult.PERMANENT_FAILURE, MonitoringRequestResult.UNAVAILABLE -> Result.failure()
        }
    }
}
