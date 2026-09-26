package com.example.tvseriestracker.data.remote

import com.example.tvseriestracker.data.Series
import com.example.tvseriestracker.data.SeriesMetadataEntity
import java.time.LocalDate

private fun String?.asDate(): LocalDate? = this?.takeIf { it.isNotBlank() }
    ?.let { runCatching { LocalDate.parse(it) }.getOrNull() }

fun TvSeriesDto.toSeries(internalId: String, legacy: Series? = null): Series? {
    val tmdbId = id?.takeIf { it > 0 } ?: return null
    val displayTitle = name?.takeIf { it.isNotBlank() } ?: return null
    val date = firstAirDate.asDate()
    return (legacy ?: Series(
        id = internalId,
        title = displayTitle,
        platform = "",
    )).copy(
        tmdbId = tmdbId,
        title = if (legacy == null) displayTitle else legacy.title,
        originalTitle = originalName?.takeIf { it.isNotBlank() } ?: displayTitle,
        firstAirDate = date,
        year = date?.year ?: legacy?.year,
        overview = overview?.takeIf { it.isNotBlank() } ?: legacy?.overview,
        posterPath = posterPath ?: legacy?.posterPath,
        backdropPath = backdropPath ?: legacy?.backdropPath
    )
}

fun TvSeriesDto.toMetadata(internalId: String, fallback: Series): SeriesMetadataEntity {
    val date = firstAirDate.asDate()
    return SeriesMetadataEntity(
        id = internalId,
        tmdbId = requireNotNull(id ?: fallback.tmdbId),
        title = name?.takeIf { it.isNotBlank() } ?: fallback.title,
        originalTitle = originalName?.takeIf { it.isNotBlank() } ?: fallback.originalTitle,
        firstAirDate = date?.toString() ?: fallback.firstAirDate?.toString(),
        year = date?.year ?: fallback.year,
        overview = overview?.takeIf { it.isNotBlank() } ?: fallback.overview,
        posterPath = posterPath ?: fallback.posterPath,
        backdropPath = backdropPath ?: fallback.backdropPath,
        platform = networks?.mapNotNull { it.name?.takeIf(String::isNotBlank) }?.distinct()?.joinToString(" / ")?.takeIf { it.isNotBlank() }
            ?: fallback.platform.takeIf { it.isNotBlank() }
    )
}

fun Series.toMetadata(): SeriesMetadataEntity = SeriesMetadataEntity(
    id = id,
    tmdbId = requireNotNull(tmdbId),
    title = title,
    originalTitle = originalTitle,
    firstAirDate = firstAirDate?.toString(),
    year = year,
    overview = overview,
    posterPath = posterPath,
    backdropPath = backdropPath,
    platform = platform.takeIf { it.isNotBlank() }
)

fun SeriesMetadataEntity.toSeries(legacy: Series?, tracked: Boolean): Series {
    val base = legacy ?: Series(id = id, title = title, platform = "")
    return base.copy(
        tmdbId = tmdbId,
        title = if (legacy == null) title else legacy.title,
        originalTitle = originalTitle,
        firstAirDate = firstAirDate.asDate(),
        year = year ?: base.year,
        overview = overview,
        posterPath = posterPath ?: base.posterPath,
        backdropPath = backdropPath,
        platform = if (legacy == null) platform.orEmpty() else base.platform,
        isTracked = tracked
    )
}
