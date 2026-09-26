package com.example.tvseriestracker.data

import java.time.LocalDate

enum class SeriesStatus { RENEWED, RELEASE_DATE_CONFIRMED, FINAL_SEASON, CANCELED }

data class Series(
    val id: String,
    val title: String,
    val originalTitle: String = title,
    val year: Int? = null,
    val platform: String,
    val tmdbId: Int? = null,
    val firstAirDate: LocalDate? = null,
    val overview: String? = null,
    val posterPath: String? = null,
    val backdropPath: String? = null,
    val nextSeasonNumber: Int? = null,
    val status: SeriesStatus? = null,
    val official: OfficialSeriesData? = null,
    val releaseDate: LocalDate? = null,
    val releaseYear: Int? = null,
    val officialSourceName: String? = null,
    val officialSourceUrl: String? = null,
    val officialAnnouncementDate: LocalDate? = null,
    val lastChecked: LocalDate? = null,
    val posterIdentifier: String? = null,
    val isTracked: Boolean = false
)

/** Legacy title/poster catalog. Only Official Data may supply season status or dates. */
object DemoCatalog {
    val initialIds = listOf("tulsa-king", "mayor-of-kingstown", "lioness", "mobland", "landman", "fallout", "the-night-agent", "silo", "dutton-ranch", "the-white-lotus", "the-gentlemen", "the-madison")
    // Verified against exact TMDB TV IDs and first-air years; never fuzzy-match legacy rows.
    val tmdbIds = mapOf(
        "tulsa-king" to 153312, "mayor-of-kingstown" to 97951, "lioness" to 113962,
        "mobland" to 247718, "landman" to 157741, "fallout" to 106379,
        "the-night-agent" to 129552, "silo" to 125988, "dutton-ranch" to 299167,
        "the-white-lotus" to 111803, "the-gentlemen" to 236235, "the-madison" to 225891,
        "severance" to 95396, "the-last-of-us" to 100088,
        "slow-horses" to 95480, "stranger-things" to 66732
    )
    private val posterPaths = mapOf(
        "tulsa-king" to "/rOYLWCdAifpUtPlTf1WHxyaxeMt.jpg",
        "mayor-of-kingstown" to "/6rWIip9MZELAA0SKii5WqsBDCYW.jpg",
        "lioness" to "/rzpHPSEgPTpRs8EHbygwsOw7jC0.jpg",
        "mobland" to "/5Xc7WpWsgflfgEMoBlf9TmWhfbH.jpg",
        "landman" to "/hYthRgS1nvQkGILn9YmqsF8kSk6.jpg",
        "fallout" to "/c15BtJxCXMrISLVmysdsnZUPQft.jpg",
        "the-night-agent" to "/4c5yUNcaff4W4aPrkXE6zr7papX.jpg",
        "silo" to "/gMYZZvnkVNTqSVnVCphWbPXwWwb.jpg",
        "dutton-ranch" to "/xsiecCxd8lkcAluw0wWwbW5CwSv.jpg",
        "the-white-lotus" to "/gbSaK9v1CbcYH1ISgbM7XObD2dW.jpg",
        "the-gentlemen" to "/tw3tzfXaSpmUZIB8ZNqNEGzMBCy.jpg",
        "the-madison" to "/nZVRyqVbDqfLSOrLcsGGTUHccZ8.jpg",
        "severance" to "/pPHpeI2X1qEd1CS1SeyrdhZ4qnT.jpg",
        "the-last-of-us" to "/dmo6TYuuJgaYinXBPjrgG9mB5od.jpg",
        "slow-horses" to "/AdYr4DjOgXvDUMwu6vEhZy1Rnxk.jpg",
        "stranger-things" to "/uOOtwVbSr4QDjAGIifLDwpb2Pdl.jpg"
    )
    val series = listOf(
        Series("tulsa-king", "Tulsa King", year = 2022, platform = "Paramount+"),
        Series("mayor-of-kingstown", "Mayor of Kingstown", year = 2021, platform = "Paramount+"),
        Series("lioness", "Lioness", year = 2023, platform = "Paramount+"),
        Series("mobland", "MobLand", year = 2025, platform = "Paramount+"),
        Series("landman", "Landman", year = 2024, platform = "Paramount+"),
        Series("fallout", "Fallout", year = 2024, platform = "Prime Video"),
        Series("the-night-agent", "The Night Agent", year = 2023, platform = "Netflix"),
        Series("silo", "Silo", year = 2023, platform = "Apple TV+"),
        Series("dutton-ranch", "Dutton Ranch", platform = "Paramount+"),
        Series("the-white-lotus", "The White Lotus", year = 2021, platform = "HBO / Max"),
        Series("the-gentlemen", "The Gentlemen", year = 2024, platform = "Netflix"),
        Series("the-madison", "The Madison", platform = "Paramount+"),
        Series("severance", "Severance", year = 2022, platform = "Apple TV+"),
        Series("the-last-of-us", "The Last of Us", year = 2023, platform = "HBO / Max"),
        Series("slow-horses", "Slow Horses", year = 2022, platform = "Apple TV+"),
        Series("stranger-things", "Stranger Things", year = 2016, platform = "Netflix")
    ).map { it.copy(tmdbId = tmdbIds[it.id], posterPath = posterPaths[it.id]) }
}
