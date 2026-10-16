package com.example.tvseriestracker.data
/** Type doubles only; actual MonitoringTracking.kt is compiled without edits. */
enum class AppLanguage { HR, EN }
interface SeriesRepository { suspend fun track(series: Series, language: AppLanguage) }
