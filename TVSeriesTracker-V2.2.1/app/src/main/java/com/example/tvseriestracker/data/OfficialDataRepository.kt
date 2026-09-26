package com.example.tvseriestracker.data

import android.content.Context
import android.util.Log
import com.example.tvseriestracker.BuildConfig
import java.io.IOException
import java.time.Instant
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request

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
    private val now: () -> Instant = Instant::now
) {
    val cache: Flow<OfficialCache> = store.cache

    suspend fun bootstrap() {
        val payload = OfficialDataParser.parse(bundled())
        val cached = store.snapshot().generatedAt
        // A new APK may carry newer verified data than the previous bundled cache.
        // Never replace a still newer remote cache with an older APK asset.
        if (cached == null || payload.generatedAt > cached) store.replace(payload, Instant.EPOCH)
    }

    suspend fun refresh(): Boolean {
        val json = remote.fetch()
        val payload = try { OfficialDataParser.parse(json) } catch (error: Exception) {
            if (BuildConfig.DEBUG) Log.d("OfficialData", "Rejected remote payload: ${error.message}")
            throw error
        }
        val current = store.snapshot()
        return if (current.generatedAt == null || payload.generatedAt > current.generatedAt) {
            store.replace(payload, now())
            true
        } else {
            store.touch(now())
            false
        }
    }

    suspend fun refreshIfStale() {
        val last = store.snapshot().lastSyncedAt
        if (last == null || last.plusSeconds(24 * 60 * 60).isBefore(now())) {
            try { refresh() } catch (error: Exception) {
                if (BuildConfig.DEBUG) Log.d("OfficialData", "Automatic refresh failed: ${error.message}")
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
    private val client = OkHttpClient()
    override suspend fun fetch(): String = withContext(Dispatchers.IO) {
        if (url.isBlank()) throw IOException("OFFICIAL_DATA_URL is not configured")
        require(OfficialDataParser.isSafeHttpsUrl(url)) { "OFFICIAL_DATA_URL must be HTTPS" }
        client.newCall(Request.Builder().url(url).get().build()).execute().use { response ->
            if (!response.isSuccessful) throw IOException("Official Data HTTP ${response.code}")
            response.body?.string() ?: throw IOException("Empty Official Data response")
        }
    }
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
