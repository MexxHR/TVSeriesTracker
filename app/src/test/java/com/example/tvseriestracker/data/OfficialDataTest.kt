package com.example.tvseriestracker.data

import com.google.gson.JsonParser
import java.io.File
import java.io.IOException
import java.time.Instant
import java.time.LocalDate
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class OfficialDataTest {
    private fun json(status: String = "RENEWED", releaseDate: String? = null,
        url: String = "https://www.paramountpressexpress.com/example", generatedAt: String = "2026-09-26T12:00:00Z") = """
        {"schemaVersion":1,"generatedAt":"$generatedAt","series":[{"tmdbId":153312,
        "title":"Tulsa King","nextSeasonNumber":4,"status":"$status",
        "releaseDate":${releaseDate?.let { "\"$it\"" } ?: "null"},"releaseYear":null,
        "sourceName":"Paramount Press Express","sourceUrl":"$url",
        "announcementDate":"2026-09-20","lastChecked":"2026-09-26"}]}
    """.trimIndent()

    @Test fun validPayloadParses() {
        val item = OfficialDataParser.parse(json()).series.single()
        assertEquals(153312, item.tmdbId)
        assertEquals(SeriesStatus.RENEWED, item.status)
        assertEquals(OfficialSourceType.OFFICIAL, item.sourceType)
    }

    @Test fun unknownStatusRejected() = assertRejected(json(status = "RUMORED"))
    @Test fun confirmedDateRequired() = assertRejected(json(status = "RELEASE_DATE_CONFIRMED"))
    @Test fun nonHttpsSourceRejected() = assertRejected(json(url = "http://example.com"))

    @Test fun remoteFailurePreservesCache() = runBlocking {
        val store = FakeStore()
        val old = OfficialDataParser.parse(json())
        store.replace(old, Instant.EPOCH)
        val repository = OfficialDataRepository(store, object : OfficialRemoteSource {
            override suspend fun fetch(): String = throw IOException("offline")
        }, { "" })
        try { repository.refresh(); fail("Expected network failure") } catch (_: IOException) { }
        assertEquals(old.series, store.snapshot().series)
    }

    @Test fun newerPayloadReplacesCache() = runBlocking {
        val store = FakeStore()
        store.replace(OfficialDataParser.parse(json()), Instant.EPOCH)
        val repository = OfficialDataRepository(store, object : OfficialRemoteSource {
            override suspend fun fetch() = json(status = "FINAL_SEASON", generatedAt = "2026-09-27T12:00:00Z")
        }, { "" })
        assertTrue(repository.refresh())
        assertEquals(SeriesStatus.FINAL_SEASON, store.snapshot().series.single().status)
    }

    @Test fun equalTimestampIsUpToDateWithoutReplacingCache() = runBlocking {
        val store = FakeStore()
        store.replace(OfficialDataParser.parse(json()), Instant.EPOCH)
        val repository = OfficialDataRepository(store, object : OfficialRemoteSource {
            override suspend fun fetch() = json(status = "FINAL_SEASON")
        }, { "" }, { Instant.parse("2026-09-27T12:00:00Z") })
        assertFalse(repository.refresh())
        assertEquals(SeriesStatus.RENEWED, store.snapshot().series.single().status)
        assertEquals(Instant.parse("2026-09-27T12:00:00Z"), store.snapshot().lastSyncedAt)
    }

    @Test fun finalSeasonCanHaveConfirmedDate() {
        val silo = bundled().series.single { it.tmdbId == DemoCatalog.tmdbIds["silo"] }
        assertEquals(SeriesStatus.FINAL_SEASON, silo.status)
        assertEquals(LocalDate.of(2027, 7, 9), silo.releaseDate)
    }

    @Test fun siloAppearsUpcomingWithFinalSeasonBadge() {
        val silo = Series("silo", "Silo", platform = "Apple TV+", tmdbId = 125988)
            .withOfficial(bundled().series.single { it.tmdbId == 125988 })
        assertTrue(silo.hasUpcomingOfficialDate(LocalDate.of(2026, 9, 26)))
        assertEquals(SeriesStatus.FINAL_SEASON, silo.status)
    }

    @Test fun missingOfficialRecordRemainsNotVerified() {
        val lioness = Series("lioness", "Lioness", platform = "Paramount+", tmdbId = 113962)
            .withOfficial(bundled().series.firstOrNull { it.tmdbId == 113962 })
        assertNull(lioness.official)
        assertNull(lioness.status)
        assertFalse(lioness.hasUpcomingOfficialDate(LocalDate.of(2026, 9, 26)))
    }

    @Test fun bundledVerifiedDatasetPassesValidator() {
        val payload = bundled()
        assertEquals(10, payload.series.size)
        assertEquals(Instant.parse("2026-09-27T07:00:00Z"), payload.generatedAt)
        assertEquals(10, payload.series.map { it.tmdbId }.toSet().size)
    }

    @Test fun duplicateTmdbIdRejected() {
        val root = JsonParser.parseString(bundledText()).asJsonObject
        val rows = root.getAsJsonArray("series")
        rows.add(rows[0].deepCopy())
        assertRejected(root.toString())
    }

    @Test fun newerBundledDatasetUpgradesOldEmptyCache() = runBlocking {
        val store = FakeStore()
        store.replace(OfficialPayload(Instant.parse("2026-09-26T00:00:00Z"), emptyList()), Instant.EPOCH)
        val repository = OfficialDataRepository(store, object : OfficialRemoteSource {
            override suspend fun fetch(): String = error("Remote not needed")
        }, { bundledText() })
        repository.bootstrap()
        assertEquals(10, store.snapshot().series.size)
    }

    @Test fun olderBundledDatasetDoesNotOverwriteNewerCache() = runBlocking {
        val store = FakeStore()
        store.replace(OfficialPayload(Instant.parse("2026-09-27T08:00:00Z"), emptyList()), Instant.EPOCH)
        val repository = OfficialDataRepository(store, object : OfficialRemoteSource {
            override suspend fun fetch(): String = error("Remote not needed")
        }, { bundledText() })
        repository.bootstrap()
        assertTrue(store.snapshot().series.isEmpty())
    }

    private fun bundled(): OfficialPayload = OfficialDataParser.parse(bundledText())
    private fun bundledText(): String {
        val fromModule = File("src/main/assets/official_series_data.json")
        return (if (fromModule.exists()) fromModule else File("app/src/main/assets/official_series_data.json")).readText()
    }

    private fun assertRejected(payload: String) {
        try { OfficialDataParser.parse(payload); fail("Expected invalid payload") }
        catch (_: IllegalArgumentException) { }
    }

    private class FakeStore : OfficialDataStore {
        private val state = MutableStateFlow(OfficialCache(null, null, emptyList()))
        override val cache = state
        override suspend fun snapshot() = state.value
        override suspend fun replace(payload: OfficialPayload, syncedAt: Instant) {
            state.value = OfficialCache(payload.generatedAt, syncedAt, payload.series)
        }
        override suspend fun touch(syncedAt: Instant) {
            state.value = state.value.copy(lastSyncedAt = syncedAt)
        }
    }
}
