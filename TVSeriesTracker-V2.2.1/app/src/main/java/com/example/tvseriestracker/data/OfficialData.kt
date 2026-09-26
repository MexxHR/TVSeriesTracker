package com.example.tvseriestracker.data

import com.google.gson.JsonObject
import com.google.gson.JsonParser
import java.net.URI
import java.time.Instant
import java.time.LocalDate

enum class OfficialSourceType { OFFICIAL }

data class OfficialSeriesData(
    val tmdbId: Int,
    val nextSeasonNumber: Int?,
    val status: SeriesStatus,
    val releaseDate: LocalDate?,
    val releaseYear: Int?,
    val sourceName: String,
    val sourceUrl: String,
    val announcementDate: LocalDate?,
    val lastChecked: LocalDate,
    val sourceType: OfficialSourceType = OfficialSourceType.OFFICIAL
)

data class OfficialPayload(val generatedAt: Instant, val series: List<OfficialSeriesData>)

/** Reject the complete payload on any bad row, so a partial download cannot erase valid cache. */
object OfficialDataParser {
    fun parse(json: String): OfficialPayload {
        val root = JsonParser.parseString(json).asJsonObject
        require(root.int("schemaVersion") == 1) { "Unsupported Official Data schema" }
        val generatedAt = Instant.parse(root.required("generatedAt"))
        val rows = root.getAsJsonArray("series") ?: error("Missing series array")
        val seen = mutableSetOf<Int>()
        val data = rows.map { element ->
            val item = element.asJsonObject
            val tmdbId = item.int("tmdbId")
            require(tmdbId > 0 && seen.add(tmdbId)) { "Invalid or duplicate tmdbId" }
            item.required("title") // Editorial hint only; TMDB supplies the display title.
            val status = SeriesStatus.valueOf(item.required("status"))
            val sourceName = item.required("sourceName")
            val sourceUrl = item.required("sourceUrl")
            require(isSafeHttpsUrl(sourceUrl)) { "Invalid official source URL" }
            val lastChecked = LocalDate.parse(item.required("lastChecked"))
            val date = item.optional("releaseDate")?.let(LocalDate::parse)
            val year = item.optionalInt("releaseYear")
            require(year == null || year in 1900..2100) { "Invalid release year" }
            require(date == null || year == null || date.year == year) { "Contradictory release date and year" }
            require(status != SeriesStatus.RELEASE_DATE_CONFIRMED || date != null) { "Confirmed date is missing" }
            val season = item.optionalInt("nextSeasonNumber")
            require(season == null || season > 0) { "Invalid season number" }
            OfficialSeriesData(tmdbId, season, status, date, year, sourceName, sourceUrl,
                item.optional("announcementDate")?.let(LocalDate::parse), lastChecked)
        }
        return OfficialPayload(generatedAt, data)
    }

    fun isSafeHttpsUrl(value: String): Boolean = runCatching {
        val uri = URI(value)
        uri.scheme.equals("https", ignoreCase = true) && !uri.host.isNullOrBlank() &&
            uri.userInfo == null && uri.port != 0 && !value.any { it.isWhitespace() }
    }.getOrDefault(false)

    private fun JsonObject.required(key: String): String = optional(key)?.takeIf { it.isNotBlank() }
        ?: error("Missing $key")
    private fun JsonObject.optional(key: String): String? = get(key)?.takeUnless { it.isJsonNull }?.asString
    private fun JsonObject.int(key: String): Int = get(key)?.asInt ?: error("Missing $key")
    private fun JsonObject.optionalInt(key: String): Int? = get(key)?.takeUnless { it.isJsonNull }?.asInt
}

fun Series.withOfficial(data: OfficialSeriesData?): Series = copy(
    official = data,
    status = data?.status,
    nextSeasonNumber = data?.nextSeasonNumber,
    releaseDate = data?.releaseDate,
    releaseYear = data?.releaseYear,
    officialSourceName = data?.sourceName,
    officialSourceUrl = data?.sourceUrl,
    officialAnnouncementDate = data?.announcementDate,
    lastChecked = data?.lastChecked
)

/** A verified date is independent of whether the season is renewed or final. */
fun Series.hasUpcomingOfficialDate(today: LocalDate): Boolean =
    official != null && releaseDate?.isAfter(today) == true
