package com.example.tvseriestracker.data

import com.example.tvseriestracker.data.remote.MonitoringRequestQueue
import kotlinx.coroutines.CancellationException

/** A successful local insert is independent of transport queue availability. */
suspend fun trackAndQueueMonitoring(
    repository: SeriesRepository, queue: MonitoringRequestQueue, item: Series, language: AppLanguage,
    obligations: MonitoringObligations? = null
) {
    if (obligations != null) {
        obligations.addBoundary { trackPrepared(repository, queue, item, language, obligations) }
    } else trackPrepared(repository, queue, item, language, null)
}

private suspend fun trackPrepared(repository: SeriesRepository, queue: MonitoringRequestQueue,
    item: Series, language: AppLanguage, obligations: MonitoringObligations?) {
    // Write-ahead prepare closes process death between Room insert and activation.
    // PREPARED is never submitted until Add commits or startup observes it followed.
    val prepared = item.tmdbId?.let { id ->
        try { obligations?.prepare(id, item.id) }
        catch (cancel: CancellationException) { throw cancel }
        catch (failure: Exception) { repository.track(item, language); throw failure }
    }
    repository.track(item, language)
    if (prepared != null) obligations?.activate(prepared)
    item.tmdbId?.let {
        try { queue.ensure(it) } catch (cancel: CancellationException) { throw cancel }
        catch (_: Exception) { /* Durable SUBMIT remains for later repair. */ }
    }
}
