package com.example.tvseriestracker.data.remote

import java.io.IOException
import java.net.URL
import java.time.Instant
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class MonitoringProtocolTest {
    private class FakeTransport : MonitoringHttpTransport {
        val calls = mutableListOf<Call>()
        val responses = ArrayDeque<MonitoringHttpResponse>()
        var failure: Exception? = null

        override fun execute(url: URL, body: ByteArray, timeoutMillis: Int): MonitoringHttpResponse {
            calls += Call(url.toString(), body.toString(Charsets.UTF_8), timeoutMillis)
            failure?.let { throw it }
            return responses.removeFirst()
        }
    }

    private data class Call(val url: String, val body: String, val timeoutMillis: Int)

    private val receipt = "0123456789abcdef".repeat(4)

    private fun response(code: Int, body: String, headers: Map<String, List<String>> = emptyMap()) =
        MonitoringHttpResponse(code, headers, body.toByteArray(Charsets.UTF_8))

    private fun protocol(fake: FakeTransport, now: Long = 1_000_000L) = HttpMonitoringProtocol(
        "https://backend.example/api/", fake, { now },
    )

    @Test fun submitsOnlyTmdbIdAndAcceptsBatchReceipt() = runBlocking {
        val fake = FakeTransport().apply {
            responses += response(202, """{"accepted":true,"tmdbId":123,"state":"queued","requestReceipt":"$receipt"}""")
        }
        val result = protocol(fake).submit(123)
        assertEquals(SubmissionReply(true, receipt), result)
        assertEquals("https://backend.example/api/v1/series-requests", fake.calls.single().url)
        assertEquals("{\"tmdbId\":123}", fake.calls.single().body)
        assertEquals(8_000, fake.calls.single().timeoutMillis)
        assertFalse(result.toString().contains(receipt))
    }

    @Test fun legacyAcceptedWithoutReceiptRemainsValid() = runBlocking {
        val fake = FakeTransport().apply {
            responses += response(202, """{"accepted":true,"tmdbId":7,"state":"already_queued"}""")
        }
        assertEquals(SubmissionReply(accepted = true), protocol(fake).submit(7))
    }

    @Test fun submitsWithOptionalReceiptAndExactStatusBody() = runBlocking {
        val fake = FakeTransport().apply {
            responses += response(202, """{"accepted":true,"tmdbId":9,"state":"queued","requestReceipt":"$receipt"}""")
            responses += response(200, """{"tmdbId":9,"state":"pending"}""")
        }
        val api = protocol(fake)
        assertEquals(receipt, api.submit(9).receipt)
        assertEquals(ReceiptReply(ReceiptState.PENDING), api.status(9, receipt))
        assertEquals("https://backend.example/api/v1/series-requests/status", fake.calls.last().url)
        assertEquals("{\"tmdbId\":9,\"requestReceipt\":\"$receipt\"}", fake.calls.last().body)
    }

    @Test fun parsesEachExactReceiptStatusState() = runBlocking {
        for ((wire, expected) in listOf(
            "pending" to ReceiptState.PENDING,
            "complete" to ReceiptState.COMPLETE,
            "unknown" to ReceiptState.UNKNOWN,
        )) {
            val fake = FakeTransport().apply { responses += response(200, """{"tmdbId":31,"state":"$wire"}""") }
            assertEquals(expected, protocol(fake).status(31, receipt).state)
        }
    }

    @Test fun transientHttpStatusReturnsRetryableTransportResultAndRetryAfterSeconds() = runBlocking {
        val fake = FakeTransport().apply {
            responses += response(429, "", mapOf("Retry-After" to listOf("37")))
            responses += response(503, "", mapOf("retry-after" to listOf("999999")))
        }
        val api = protocol(fake)
        assertEquals(SubmissionReply(false, retryAfterMillis = 37_000L), api.submit(8))
        assertEquals(ReceiptReply(ReceiptState.TEMPORARY_FAILURE, 6 * 60 * 60 * 1000L), api.status(8, receipt))
    }

    @Test fun parsesRetryAfterRfc1123DateAndIgnoresMalformedValue() = runBlocking {
        val now = Instant.parse("2026-10-16T12:00:00Z").toEpochMilli()
        val date = DateTimeFormatter.RFC_1123_DATE_TIME.withLocale(Locale.US)
            .format(Instant.ofEpochMilli(now + 90_000L).atZone(java.time.ZoneOffset.UTC))
        val fake = FakeTransport().apply {
            responses += response(429, "", mapOf("Retry-After" to listOf(date)))
            responses += response(429, "", mapOf("Retry-After" to listOf("soon")))
        }
        val api = HttpMonitoringProtocol("https://backend.example", fake, { now })
        assertEquals(90_000L, api.status(8, receipt).retryAfterMillis)
        assertNull(api.status(8, receipt).retryAfterMillis)
    }

    @Test fun statusTransientAndSubmissionNetworkFailureNeverBecomeUnknownOrAccepted() = runBlocking {
        val fake = FakeTransport().apply { failure = IOException("private diagnostic") }
        val api = protocol(fake)
        assertEquals(SubmissionReply(accepted = false), api.submit(8))
        assertEquals(ReceiptReply(ReceiptState.TEMPORARY_FAILURE), api.status(8, receipt))
    }

    @Test fun rejectsMismatchedIdsMalformedReceiptsDuplicateAndUnknownFields() = runBlocking {
        val invalidBodies = listOf(
            """{"accepted":true,"tmdbId":4,"state":"queued"}""" to true, // mismatch
            """{"accepted":true,"tmdbId":5,"state":"queued","requestReceipt":"bad"}""" to true,
            """{"accepted":true,"tmdbId":5,"state":"queued","x":1}""" to true,
            """{"accepted":true,"accepted":true,"tmdbId":5,"state":"queued"}""" to true,
            """{"accepted":true,"tmdbId":5,"state":"queued"} {}""" to true,
        )
        for ((body, _) in invalidBodies) {
            val fake = FakeTransport().apply { responses += response(202, body) }
            val failure = runCatching { protocol(fake).submit(5) }.exceptionOrNull()
            assertTrue(failure is MonitoringProtocolException)
            assertFalse(failure!!.message.orEmpty().contains(receipt))
        }

        val badStatus = FakeTransport().apply {
            responses += response(200, """{"tmdbId":5,"state":"unknown","extra":false}""")
        }
        assertTrue(runCatching { protocol(badStatus).status(5, receipt) }.exceptionOrNull() is MonitoringProtocolException)
    }

    @Test fun rejectsOversizedResponsesAndNonTransientHttpErrors() = runBlocking {
        val oversized = FakeTransport().apply { responses += MonitoringHttpResponse(200, emptyMap(), ByteArray(8193)) }
        assertTrue(runCatching { protocol(oversized).status(5, receipt) }.exceptionOrNull() is MonitoringProtocolException)

        val unauthorized = FakeTransport().apply { responses += response(401, "") }
        val failure = runCatching { protocol(unauthorized).status(5, receipt) }.exceptionOrNull()
        assertTrue(failure is MonitoringProtocolException)
        assertFalse((failure as MonitoringProtocolException).retryable)
    }

    @Test fun rejectsUnsafeOriginsIdsAndReceiptsWithoutMakingRequests() = runBlocking {
        val fake = FakeTransport()
        assertTrue(runCatching { HttpMonitoringProtocol("http://backend.example", fake) }.isFailure)
        assertTrue(runCatching { HttpMonitoringProtocol("https://user@backend.example", fake) }.isFailure)
        assertTrue(runCatching { protocol(fake).submit(0) }.exceptionOrNull() is MonitoringProtocolException)
        assertTrue(runCatching { protocol(fake).status(5, "not-a-receipt") }.exceptionOrNull() is MonitoringProtocolException)
        assertTrue(fake.calls.isEmpty())
    }
}
