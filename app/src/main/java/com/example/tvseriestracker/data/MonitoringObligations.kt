package com.example.tvseriestracker.data

import com.example.tvseriestracker.data.remote.MonitoringProtocol
import com.example.tvseriestracker.data.remote.ReceiptState
import java.util.Base64
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull

enum class MonitoringPhase { PREPARED, SUBMIT, RECEIPT, CANONICAL, LEGACY }

/** Local key rejects callbacks from a removed/re-added intent; never sent to the server. */
data class MonitoringIntent(
    val tmdbId: Int, val seriesId: String, val localKey: String,
    val phase: MonitoringPhase = MonitoringPhase.PREPARED,
    val receipt: String? = null, val nextAt: Long = 0, val legacyAttempts: Int = 0
) {
    override fun toString() = "MonitoringIntent(tmdbId=$tmdbId, phase=$phase)"
}

interface MonitoringIntentStore {
    val changes: Flow<Map<Int, MonitoringIntent>>
    suspend fun snapshot(): Map<Int, MonitoringIntent>
    suspend fun update(transform: (Map<Int, MonitoringIntent>) -> Map<Int, MonitoringIntent>)
    suspend fun reserveStatusWindow(now: Long): Boolean
    suspend fun statusReadyAt(): Long
    suspend fun reserveSubmissionDelay(now: Long): Long
}

/** Bounded versioned line records, in a separate preferences file (not settings/Room). */
object MonitoringIntentCodec {
    const val MAX_ITEMS = 1000
    fun encode(item: MonitoringIntent): String {
        require(valid(item))
        val id = Base64.getUrlEncoder().withoutPadding().encodeToString(item.seriesId.toByteArray(Charsets.UTF_8))
        return listOf("1", item.tmdbId, id, item.localKey, item.phase.name,
            item.receipt ?: "-", item.nextAt, item.legacyAttempts).joinToString("|")
    }
    fun decode(text: String): MonitoringIntent? = try {
        require(text.length <= 2048)
        val parts = text.split('|'); require(parts.size == 8 && parts[0] == "1")
        val item = MonitoringIntent(parts[1].toInt(), String(Base64.getUrlDecoder().decode(parts[2]), Charsets.UTF_8),
            parts[3], MonitoringPhase.valueOf(parts[4]), parts[5].takeUnless { it == "-" },
            parts[6].toLong(), parts[7].toInt())
        require(valid(item)); item
    } catch (_: Exception) { null }

    fun valid(item: MonitoringIntent): Boolean = item.tmdbId > 0 && item.seriesId.length in 1..256 &&
        item.localKey.matches(Regex("[0-9a-f]{32}")) && item.nextAt >= 0 && item.legacyAttempts in 0..3 &&
        (item.receipt == null || item.receipt.matches(Regex("[0-9a-f]{64}"))) &&
        ((item.phase == MonitoringPhase.RECEIPT || item.phase == MonitoringPhase.CANONICAL) == (item.receipt != null))
}

class MonitoringObligations(val store: MonitoringIntentStore, private val clock: () -> Long = System::currentTimeMillis) {
    private val addMutex = Mutex()
    suspend fun <T> addBoundary(block: suspend () -> T): T = addMutex.withLock { block() }
    suspend fun prepare(id: Int, seriesId: String): MonitoringIntent {
        var result: MonitoringIntent? = null
        store.update { old ->
            val existing = old[id]
            // Duplicate Add coalesces one existing obligation, including its receipt.
            val item = existing ?: MonitoringIntent(id, seriesId, UUID.randomUUID().toString().replace("-", ""))
            require(MonitoringIntentCodec.valid(item))
            require(existing != null || old.size < MonitoringIntentCodec.MAX_ITEMS)
            result = item; old + (id to item)
        }
        return requireNotNull(result)
    }
    suspend fun change(item: MonitoringIntent, transform: (MonitoringIntent) -> MonitoringIntent?): Boolean {
        var applied = false
        store.update { old ->
            if (old[item.tmdbId] != item) old else {
                applied = true
                val updated = transform(item)
                if (updated == null) old - item.tmdbId else old + (item.tmdbId to updated)
            }
        }
        return applied
    }
    suspend fun activate(item: MonitoringIntent) {
        if (item.phase == MonitoringPhase.PREPARED) change(item) { it.copy(phase = MonitoringPhase.SUBMIT, nextAt = clock()) }
    }
    suspend fun removeSeries(seriesId: String) = store.update { old -> old.filterValues { it.seriesId != seriesId } }
    suspend fun repairPrepared(followed: suspend () -> Set<String>) = addMutex.withLock {
        val ids = followed()
        store.update { old -> old.filterValues { it.seriesId in ids }
            .mapValues { (_, item) ->
                if (item.phase == MonitoringPhase.PREPARED ||
                    (item.phase == MonitoringPhase.LEGACY && item.legacyAttempts == 3 && item.nextAt <= clock()))
                    item.copy(phase = MonitoringPhase.SUBMIT, nextAt = clock(), legacyAttempts = 0)
                else item
            } }
    }
}

/** Transport reserves a durable cooldown before any POST/query/enqueue uncertainty. */
class MonitoringSubmission(
    private val obligations: MonitoringObligations,
    private val protocol: MonitoringProtocol,
    private val clock: () -> Long = System::currentTimeMillis
) {
    suspend fun submit(id: Int): Boolean {
        val item = obligations.store.snapshot()[id] ?: return true
        if (item.phase != MonitoringPhase.SUBMIT || item.nextAt > clock()) return true
        val quotaDelay = obligations.store.reserveSubmissionDelay(clock())
        if (quotaDelay > 0) {
            obligations.change(item) { it.copy(nextAt = clock() + quotaDelay) }
            return true
        }
        val reserved = item.copy(nextAt = clock() + SUBMISSION_REPAIR_MILLIS)
        if (!obligations.change(item) { reserved }) return true
        try {
            val result = protocol.submit(id)
            if (result.accepted) {
                obligations.change(reserved) { it.copy(
                    phase = if (result.receipt == null) MonitoringPhase.LEGACY else MonitoringPhase.RECEIPT,
                    receipt = result.receipt, nextAt = clock() + if (result.receipt == null) 30_000 else STATUS_INTERVAL,
                    legacyAttempts = 0) }
                return true
            }
            obligations.change(reserved) { it.copy(nextAt = clock() + maxOf(SUBMISSION_REPAIR_MILLIS, result.retryAfterMillis ?: 0)) }
        } catch (cancel: CancellationException) { throw cancel }
        catch (_: Exception) { /* Reserved intent remains durable; no error payload is logged. */ }
        return false
    }
    companion object {
        const val STATUS_INTERVAL = 120_000L
        const val SUBMISSION_REPAIR_MILLIS = 20 * 60_000L
    }
}

/** One foreground timer; durable state is authoritative, Flow is only its wake-up. */
class MonitoringReconciler(
    private val scope: CoroutineScope,
    private val obligations: MonitoringObligations,
    private val protocol: MonitoringProtocol,
    private val ensureSubmission: suspend (Int) -> Unit,
    private val refreshCanonical: suspend () -> Unit,
    private val clock: () -> Long = System::currentTimeMillis
) {
    private var runner: Job? = null
    private val cycleMutex = Mutex()
    fun setActive(active: Boolean) {
        if (!active) { runner?.cancel(); runner = null; return }
        if (runner?.isActive == true) return
        runner = scope.launch {
            val wake = Channel<Unit>(Channel.CONFLATED)
            val observer = launch { obligations.store.changes.collect { wake.trySend(Unit) } }
            try {
                while (true) {
                    try { cycle() } catch (cancel: CancellationException) { throw cancel } catch (_: Exception) { }
                    val statusReady = obligations.store.statusReadyAt()
                    val next = obligations.store.snapshot().values.filter { it.phase != MonitoringPhase.PREPARED &&
                        !(it.phase == MonitoringPhase.LEGACY && it.legacyAttempts == 3) }
                        .minOfOrNull { if (it.phase == MonitoringPhase.RECEIPT) maxOf(it.nextAt, statusReady) else it.nextAt }
                    if (next == null) wake.receive()
                    else withTimeoutOrNull(maxOf(30_000L, next - clock())) { wake.receive() }
                }
            } finally { observer.cancel(); wake.close() }
        }
    }
    suspend fun cycle() = cycleMutex.withLock {
        val now = clock()
        val due = obligations.store.snapshot().values.filter { it.nextAt <= now }.sortedWith(compareBy({ it.nextAt }, { it.tmdbId }))
        for (item in due.filter { it.phase == MonitoringPhase.SUBMIT }.take(3)) {
            // Enqueue/query failure is represented by unchanged durable SUBMIT,
            // with a short repair cooldown. Worker owns the POST cooldown.
            try { ensureSubmission(item.tmdbId) } catch (cancel: CancellationException) { throw cancel } catch (_: Exception) {
                obligations.change(item) { it.copy(nextAt = now + 120_000) }
            }
        }
        // At most two status requests per 2-minute cycle = <=60/hour, leaving
        // metadata headroom. Persist reservations so restarts cannot reset it.
        val statusItems = due.filter { it.phase == MonitoringPhase.RECEIPT }.take(2)
        val admitted = statusItems.isNotEmpty() && obligations.store.reserveStatusWindow(now)
        for (item in if (admitted) statusItems else emptyList()) {
            val scheduled = item.copy(nextAt = now + MonitoringSubmission.STATUS_INTERVAL)
            if (!obligations.change(item) { scheduled }) continue
            try {
                val result = protocol.status(item.tmdbId, requireNotNull(item.receipt))
                when (result.state) {
                    ReceiptState.COMPLETE -> obligations.change(scheduled) { it.copy(phase = MonitoringPhase.CANONICAL, nextAt = clock() + 30_000) }
                    ReceiptState.UNKNOWN -> obligations.change(scheduled) { it.copy(phase = MonitoringPhase.SUBMIT, receipt = null,
                        nextAt = clock() + MonitoringSubmission.SUBMISSION_REPAIR_MILLIS) }
                    ReceiptState.PENDING, ReceiptState.TEMPORARY_FAILURE -> obligations.change(scheduled) { it.copy(
                        nextAt = clock() + maxOf(MonitoringSubmission.STATUS_INTERVAL, result.retryAfterMillis ?: 0)) }
                }
                if (result.retryAfterMillis != null) obligations.store.update { old -> old.mapValues { (_, value) ->
                    if (value.phase == MonitoringPhase.RECEIPT) value.copy(nextAt = maxOf(value.nextAt, clock() + result.retryAfterMillis)) else value } }
            } catch (cancel: CancellationException) { throw cancel } catch (_: Exception) { /* receipt retained */ }
        }
        val canonical = obligations.store.snapshot().values.filter { it.phase == MonitoringPhase.CANONICAL && it.nextAt <= clock() }
        val legacy = obligations.store.snapshot().values.filter { it.phase == MonitoringPhase.LEGACY && it.nextAt <= clock() && it.legacyAttempts < 3 }
        if (canonical.isNotEmpty() || legacy.isNotEmpty()) {
            // One fetch for all complete receipts/legacy compatibility items.
            try {
                refreshCanonical() // SAME/OLDER return normally; no factual cache rule is changed.
                for (item in canonical) obligations.change(item) { null }
            } catch (cancel: CancellationException) { throw cancel } catch (_: Exception) {
                for (item in canonical) obligations.change(item) { it.copy(nextAt = clock() + 120_000) }
            }
            for (item in legacy) obligations.change(item) {
                val attempts = it.legacyAttempts + 1
                it.copy(legacyAttempts = attempts, nextAt = if (attempts == 3) clock() + 24 * 60 * 60_000L else clock() + listOf(60_000L, 120_000L)[attempts - 1])
            }
        }
    }
}
