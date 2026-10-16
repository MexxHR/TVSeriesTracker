package com.example.tvseriestracker.data

import android.content.Context
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import java.util.Base64
import java.util.UUID
import java.io.IOException
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.monitoringIntents by preferencesDataStore("monitoring_intents_v1")

class DataStoreMonitoringIntentStore(private val disk: DataStore<Preferences>) : MonitoringIntentStore {
    constructor(context: Context) : this(context.applicationContext.monitoringIntents)
    private val statusKey = longPreferencesKey("status_not_before")
    private val submissionKey = stringPreferencesKey("submission_window")
    private fun statusTime(prefs: Preferences) = (prefs.asMap().entries.firstOrNull { it.key.name == statusKey.name }?.value as? Long)
        ?.takeIf { it >= 0 } ?: 0L
    override val changes = disk.data.map(::decode)
    override suspend fun snapshot() = decode(disk.data.first())
    private fun key(item: MonitoringIntent) = stringPreferencesKey("intent_${item.tmdbId}_" +
        Base64.getUrlEncoder().withoutPadding().encodeToString(item.seriesId.toByteArray(Charsets.UTF_8)))
    private fun decode(prefs: Preferences): Map<Int, MonitoringIntent> = buildMap {
        for ((key, value) in prefs.asMap()) {
            if (!key.name.startsWith("intent_")) continue
            val parsed = (value as? String)?.let(MonitoringIntentCodec::decode)
            val parts = key.name.split('_', limit = 3)
            val recovered = try {
                require(parts.size == 3)
                val id = parts[1].toInt()
                val seriesId = String(Base64.getUrlDecoder().decode(parts[2]), Charsets.UTF_8)
                parsed?.takeIf { it.tmdbId == id && it.seriesId == seriesId } ?: MonitoringIntent(
                    id, seriesId, UUID.randomUUID().toString().replace("-", ""))
            } catch (_: Exception) { null }
            if (recovered == null || !MonitoringIntentCodec.valid(recovered))
                throw IOException("Invalid monitoring storage identity")
            put(recovered.tmdbId, recovered)
        }
    }
    override suspend fun update(transform: (Map<Int, MonitoringIntent>) -> Map<Int, MonitoringIntent>) {
        disk.edit { prefs ->
            val updated = transform(decode(prefs))
            require(updated.size <= MonitoringIntentCodec.MAX_ITEMS && updated.all { (id, item) -> id == item.tmdbId && MonitoringIntentCodec.valid(item) })
            prefs.asMap().keys.filter { it.name.startsWith("intent_") }.forEach { prefs.remove(stringPreferencesKey(it.name)) }
            updated.values.forEach { prefs[key(it)] = MonitoringIntentCodec.encode(it) }
        }
    }
    override suspend fun reserveStatusWindow(now: Long): Boolean {
        var allowed = false
        disk.edit { prefs ->
            if (statusTime(prefs) <= now) {
                prefs[statusKey] = now + MonitoringSubmission.STATUS_INTERVAL
                allowed = true
            }
        }
        return allowed
    }
    override suspend fun statusReadyAt() = statusTime(disk.data.first())
    override suspend fun reserveSubmissionDelay(now: Long): Long {
        var delay = 0L
        disk.edit { prefs ->
            val raw = prefs.asMap().entries.firstOrNull { it.key.name == submissionKey.name }?.value as? String ?: ""
            val times = raw.split(',').mapNotNull { it.toLongOrNull() }
                .filter { it in 0..(now + 24 * 60 * 60_000L) && now - it <= MonitoringSubmission.SUBMISSION_REPAIR_MILLIS }.sorted().takeLast(3)
            if (times.size >= 3) delay = maxOf(1L, times.first() + MonitoringSubmission.SUBMISSION_REPAIR_MILLIS + 1 - now)
            else prefs[submissionKey] = (times + now).joinToString(",")
        }
        return delay
    }
}
