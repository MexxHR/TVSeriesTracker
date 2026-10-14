package com.example.tvseriestracker.data

import android.content.Context
import com.example.tvseriestracker.BuildConfig
import java.io.IOException
import java.time.Instant
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import okhttp3.Call
import okhttp3.Callback
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response

data class OfficialCache(val generatedAt: Instant?, val lastSyncedAt: Instant?, val series: List<OfficialSeriesData>)

interface OfficialDataStore {
    val cache: Flow<OfficialCache>
    suspend fun snapshot(): OfficialCache
    suspend fun replace(payload: OfficialPayload, syncedAt: Instant)
    suspend fun touch(syncedAt: Instant)
}

interface OfficialRemoteSource { suspend fun fetch(): String }

class OfficialDataRepository(
    private val store: OfficialDataStore,
    private val remote: OfficialRemoteSource,
    private val bundled: suspend () -> String,
    private val now: () -> Instant = Instant::now,
    private val refreshMutex: Mutex = Mutex(),
    private val debugLog: (String) -> Unit = { message ->
        try { android.util.Log.d("OfficialData", message) } catch (_: RuntimeException) { }
    },
    private val parsingDispatcher: CoroutineDispatcher = Dispatchers.Default
) {
    private var lastSuccessfulRefreshNanos = Long.MIN_VALUE
    private var lastSuccessfulRefreshUpdated = false
    val cache: Flow<OfficialCache> = store.cache

    suspend fun snapshot(): OfficialCache = store.snapshot()

    suspend fun bootstrap() {
        val json = bundled()
        val payload = withContext(parsingDispatcher) { OfficialDataParser.parse(json) }
        refreshMutex.withLock {
            val cached = store.snapshot().generatedAt
            // A new APK may carry newer verified data than the previous bundled cache.
            // Never replace a still newer remote cache with an older APK asset.
            if (cached == null || payload.generatedAt > cached) store.replace(payload, Instant.EPOCH)
        }
    }

    suspend fun refresh(reason: String = "unspecified"): Boolean {
        val beganAt = System.nanoTime()
        if (!refreshMutex.tryLock()) {
            return refreshMutex.withLock {
                // Simultaneous callers share a completed logical fetch instead of
                // issuing a second request immediately after the first releases the lock.
                if (lastSuccessfulRefreshNanos >= beganAt) {
                    if (BuildConfig.DEBUG) debugLog("OfficialData refresh reason=$reason decision=COALESCED")
                    lastSuccessfulRefreshUpdated
                } else refreshLocked(reason)
            }
        }
        return try { refreshLocked(reason) } finally { refreshMutex.unlock() }
    }

    private suspend fun refreshLocked(reason: String): Boolean {
        val startedAt = System.nanoTime()
        val json = try { remote.fetch() } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            if (BuildConfig.DEBUG) debugLog("OfficialData refresh failed reason=$reason error=${error.javaClass.simpleName}")
            throw error
        }
        val payload = try {
            withContext(parsingDispatcher) { OfficialDataParser.parse(json) }
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            if (BuildConfig.DEBUG) debugLog("OfficialData payload rejected reason=$reason error=${error.javaClass.simpleName}")
            throw error
        }
        val current = store.snapshot()
        val decision = when {
            current.generatedAt == null || payload.generatedAt > current.generatedAt -> "NEWER"
            payload.generatedAt == current.generatedAt -> "SAME"
            else -> "OLDER"
        }
        val updated = if (current.generatedAt == null || payload.generatedAt > current.generatedAt) {
            store.replace(payload, now())
            true
        } else {
            store.touch(now())
            false
        }
        lastSuccessfulRefreshNanos = System.nanoTime()
        lastSuccessfulRefreshUpdated = updated
        if (BuildConfig.DEBUG) debugLog("OfficialData refresh reason=$reason cached=${current.generatedAt} remote=${payload.generatedAt} decision=$decision elapsedMs=${(System.nanoTime() - startedAt) / 1_000_000}")
        return updated
    }

    suspend fun refreshIfStale() {
        val last = store.snapshot().lastSyncedAt
        if (last == null || last.plusSeconds(24 * 60 * 60).isBefore(now())) {
            try { refresh("startup") } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                if (BuildConfig.DEBUG) debugLog("OfficialData refresh failed reason=startup error=${error.javaClass.simpleName}")
            }
        }
    }
}

class RoomOfficialDataStore(private val dao: TrackingDao) : OfficialDataStore {
    override val cache: Flow<OfficialCache> = combine(dao.observeOfficial(), dao.observeOfficialSync()) { rows, sync ->
        OfficialCache(sync?.generatedAt?.let(Instant::parse), sync?.lastSyncedAt?.let(Instant::parse), rows.map { it.toDomain() })
    }

    override suspend fun snapshot(): OfficialCache {
        val sync = dao.officialSync()
        return OfficialCache(sync?.generatedAt?.let(Instant::parse), sync?.lastSyncedAt?.let(Instant::parse), dao.officialRows().map { it.toDomain() })
    }

    override suspend fun replace(payload: OfficialPayload, syncedAt: Instant) = dao.replaceOfficial(
        payload.series.map { it.toEntity() }, OfficialSyncEntity(generatedAt = payload.generatedAt.toString(), lastSyncedAt = syncedAt.toString())
    )

    override suspend fun touch(syncedAt: Instant) {
        val previous = dao.officialSync() ?: return
        dao.saveOfficialSync(previous.copy(lastSyncedAt = syncedAt.toString()))
    }
}

class HttpOfficialRemoteSource(private val url: String = BuildConfig.OFFICIAL_DATA_URL) : OfficialRemoteSource {
    private val client = OkHttpClient.Builder()
        .callTimeout(java.time.Duration.ofSeconds(20))
        .build()
    override suspend fun fetch(): String {
        if (url.isBlank()) throw IOException("OFFICIAL_DATA_URL is not configured")
        require(OfficialDataParser.isSafeHttpsUrl(url)) { "OFFICIAL_DATA_URL must be HTTPS" }
        return suspendCancellableCoroutine { continuation ->
            val call = client.newCall(Request.Builder().url(url).get().build())
            continuation.invokeOnCancellation { call.cancel() }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, error: IOException) {
                    if (continuation.isActive) continuation.resumeWithException(error)
                }
                override fun onResponse(call: Call, response: Response) {
                    try {
                        val text = response.use {
                            if (!it.isSuccessful) throw IOException("Official Data HTTP ${it.code}")
                            val body = it.body ?: throw IOException("Empty Official Data response")
                            body.source().use { source -> readBoundedOfficialData(source, body.contentLength()) }
                        }
                        if (continuation.isActive) continuation.resume(text)
                    } catch (error: Exception) {
                        if (continuation.isActive) continuation.resumeWithException(error)
                    }
                }
            })
        }
    }

    companion object { const val MAX_OFFICIAL_DATA_BYTES = 2L * 1024 * 1024 }
}

internal fun readBoundedOfficialData(source: okio.BufferedSource, contentLength: Long): String {
    if (contentLength > HttpOfficialRemoteSource.MAX_OFFICIAL_DATA_BYTES)
        throw IOException("Official Data response exceeds size limit")
    val buffer = okio.Buffer()
    while (source.read(buffer, 8192) != -1L) {
        if (buffer.size > HttpOfficialRemoteSource.MAX_OFFICIAL_DATA_BYTES)
            throw IOException("Official Data response exceeds size limit")
    }
    return buffer.readUtf8()
}

fun bundledOfficialData(context: Context): suspend () -> String = {
    withContext(Dispatchers.IO) { context.assets.open("official_series_data.json").bufferedReader().use { it.readText() } }
}

private fun OfficialDataEntity.toDomain() = OfficialSeriesData(tmdbId, nextSeasonNumber,
    SeriesStatus.valueOf(status), releaseDate?.let(java.time.LocalDate::parse), releaseYear,
    sourceName, sourceUrl, announcementDate?.let(java.time.LocalDate::parse),
    java.time.LocalDate.parse(lastChecked), OfficialSourceType.valueOf(sourceType))

private fun OfficialSeriesData.toEntity() = OfficialDataEntity(tmdbId, nextSeasonNumber, status.name,
    releaseDate?.toString(), releaseYear, sourceName, sourceUrl, announcementDate?.toString(),
    lastChecked.toString(), sourceType.name)
