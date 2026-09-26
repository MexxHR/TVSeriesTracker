package com.example.tvseriestracker.data

import com.example.tvseriestracker.data.remote.SeriesRemoteDataSource
import com.example.tvseriestracker.data.remote.toMetadata
import com.example.tvseriestracker.data.remote.toSeries
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

interface SeriesRepository {
    val tracked: Flow<List<Series>>
    fun observeSeries(id: String): Flow<Series?>
    suspend fun search(query: String, language: AppLanguage): List<Series>
    suspend fun track(series: Series, language: AppLanguage)
    suspend fun untrack(id: String)
    suspend fun refreshMetadata(id: String, language: AppLanguage)
}

class DefaultSeriesRepository(
    private val dao: TrackingDao,
    private val remote: SeriesRemoteDataSource,
    private val official: OfficialDataRepository
) : SeriesRepository {
    private val legacyById = DemoCatalog.series.associateBy { it.id }
    private val legacyByTmdb = DemoCatalog.series.mapNotNull { series -> series.tmdbId?.let { it to series } }.toMap()

    override val tracked: Flow<List<Series>> = combine(dao.observeTracked(), dao.observeMetadata(), official.cache) { rows, metadata, officialCache ->
        val metadataById = metadata.associateBy { it.id }
        val officialByTmdb = officialCache.series.associateBy { it.tmdbId }
        rows.map { row ->
            val legacy = legacyById[row.id]
            val saved = metadataById[row.id]
            val series = when {
                saved != null -> saved.toSeries(legacy, tracked = true)
                legacy != null -> legacy.copy(tmdbId = row.tmdbId ?: legacy.tmdbId, isTracked = true)
                else -> Series(row.id, row.id, platform = "", tmdbId = row.tmdbId,
                    isTracked = true)
            }
            series.withOfficial(series.tmdbId?.let(officialByTmdb::get))
        }
    }

    override fun observeSeries(id: String): Flow<Series?> = tracked.map { list -> list.firstOrNull { it.id == id } }

    override suspend fun search(query: String, language: AppLanguage): List<Series> {
        val trackedIds = dao.observeTracked().first().map { it.id }.toSet()
        val metadataById = dao.observeMetadata().first().associateBy { it.id }
        return remote.search(query, language.tmdbTag()).mapNotNull { dto ->
            val tmdbId = dto.id ?: return@mapNotNull null
            val legacy = legacyByTmdb[tmdbId]
            val id = legacy?.id ?: "tmdb-$tmdbId"
            dto.toSeries(id, legacy)?.let { series ->
                series.copy(platform = metadataById[id]?.platform ?: series.platform, isTracked = id in trackedIds)
            }
        }
    }

    override suspend fun track(series: Series, language: AppLanguage) {
        val tmdbId = series.tmdbId ?: return
        if (dao.metadataFor(series.id) == null) {
            val metadata = try {
                remote.details(tmdbId, language.tmdbTag()).toMetadata(series.id, series)
            } catch (exception: CancellationException) {
                throw exception
            } catch (_: Exception) {
                // The search result already has enough metadata to save the series offline.
                series.toMetadata()
            }
            dao.saveMetadata(metadata)
        }
        dao.add(TrackedSeries(series.id, tmdbId))
    }

    override suspend fun untrack(id: String) = dao.remove(id)

    override suspend fun refreshMetadata(id: String, language: AppLanguage) {
        if (dao.metadataFor(id) != null) return
        val row = dao.observeTracked().first().firstOrNull { it.id == id } ?: return
        val tmdbId = row.tmdbId ?: return
        val fallback = legacyById[id] ?: Series(id, id, platform = "", tmdbId = tmdbId)
        dao.saveMetadata(remote.details(tmdbId, language.tmdbTag()).toMetadata(id, fallback))
    }
}

private fun AppLanguage.tmdbTag(): String = if (this == AppLanguage.HR) "hr-HR" else "en-US"
