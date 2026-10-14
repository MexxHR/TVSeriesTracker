package com.example.tvseriestracker.data

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlin.coroutines.coroutineContext

/** One foreground batch of bounded follow-up refreshes for accepted request IDs. */
class RequestedOfficialRefresh(
    private val scope: CoroutineScope,
    private val official: OfficialDataRepository,
    private val wait: suspend (Long) -> Unit = { delay(it) },
    private val onFailure: (Exception) -> Unit = {},
    private val onDiagnostic: (String) -> Unit = {}
) {
    private val active = MutableStateFlow(false)
    private val pending = MutableStateFlow<Set<Int>>(emptySet())
    private var runner: Job? = null

    fun setActive(value: Boolean) {
        active.value = value
        if (!value) {
            runner?.cancel()
            runner = null
            pending.value = emptySet()
        }
    }

    fun requested(tmdbId: Int) {
        if (tmdbId <= 0 || !active.value) return
        pending.value = pending.value + tmdbId
        if (runner?.isActive != true) {
            runner = scope.launch {
                val thisRunner = coroutineContext[Job]
                try { run() } finally { if (runner === thisRunner) runner = null }
            }
        }
    }

    private suspend fun run() {
        var batchAttempts = 0
        while (pending.value.isNotEmpty() && batchAttempts < MAX_ADDITIONAL_ATTEMPTS) {
            active.first { it }
            discardVerified()
            if (pending.value.isEmpty()) break

            val attempt = batchAttempts + 1
            wait(RETRY_DELAYS_MILLIS[attempt - 1])
            if (!active.value) continue
            // A manual/startup refresh may have delivered the requested fact while
            // this lifecycle-scoped coroutine was waiting.
            discardVerified()
            if (pending.value.isEmpty()) break

            val requestedIds = pending.value
            val startedAt = System.nanoTime()
            onDiagnostic("OfficialData reason=requested-follow-up attempt=$attempt requestedIds=${requestedIds.sorted()}")
            try {
                official.refresh("requested-follow-up")
            } catch (exception: CancellationException) {
                throw exception
            } catch (exception: Exception) {
                onFailure(exception)
            } finally {
                batchAttempts++
            }
            discardVerified()
            onDiagnostic("OfficialData reason=requested-follow-up attempt=$attempt requestedIds=${requestedIds.sorted()} elapsedMs=${(System.nanoTime() - startedAt) / 1_000_000}")
        }
        if (batchAttempts >= MAX_ADDITIONAL_ATTEMPTS) pending.value = emptySet()
    }

    private suspend fun discardVerified() {
        val verifiedIds = try {
            official.snapshot().series.mapTo(HashSet()) { it.tmdbId }
        } catch (exception: CancellationException) {
            throw exception
        } catch (exception: Exception) {
            onFailure(exception)
            emptySet()
        }
        pending.value = pending.value.filterNot { it in verifiedIds }.toSet()
    }

    companion object {
        const val MAX_ADDITIONAL_ATTEMPTS = 3
        val RETRY_DELAYS_MILLIS = listOf(30_000L, 60_000L, 120_000L)
    }
}
