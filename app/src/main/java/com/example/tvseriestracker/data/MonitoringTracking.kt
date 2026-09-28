package com.example.tvseriestracker.data

import com.example.tvseriestracker.data.remote.MonitoringRequestQueue

/** A successful local insert is independent of transport queue availability. */
suspend fun trackAndQueueMonitoring(
    repository: SeriesRepository, queue: MonitoringRequestQueue, item: Series, language: AppLanguage
) {
    repository.track(item, language)
    item.tmdbId?.let { runCatching { queue.enqueue(it) } }
}
