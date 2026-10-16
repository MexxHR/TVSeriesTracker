package com.example.tvseriestracker.data.remote

import com.google.gson.stream.JsonReader
import com.google.gson.stream.JsonToken
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.StringReader
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.nio.charset.StandardCharsets
import java.time.Instant
import java.time.format.DateTimeFormatter
import java.time.format.DateTimeParseException
import java.util.Locale
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

data class SubmissionReply(
    val accepted: Boolean,
    val receipt: String? = null,
    val retryAfterMillis: Long? = null,
) {
    override fun toString(): String =
        "SubmissionReply(accepted=$accepted, receiptPresent=${receipt != null}, retryAfterMillis=$retryAfterMillis)"
}

enum class ReceiptState { PENDING, COMPLETE, UNKNOWN, TEMPORARY_FAILURE }

data class ReceiptReply(val state: ReceiptState, val retryAfterMillis: Long? = null)

interface MonitoringProtocol {
    suspend fun submit(tmdbId: Int): SubmissionReply
    suspend fun status(tmdbId: Int, receipt: String): ReceiptReply
}

/** A fixed, sanitized protocol failure. It deliberately carries no URL, ID, receipt, body, or cause. */
class MonitoringProtocolException internal constructor(
    val retryable: Boolean,
) : IOException(if (retryable) "Monitoring service temporarily unavailable" else "Invalid monitoring service response")

data class MonitoringHttpResponse(
    val statusCode: Int,
    val headers: Map<String, List<String>>,
    val body: ByteArray,
)

fun interface MonitoringHttpTransport {
    @Throws(IOException::class)
    fun execute(url: URL, body: ByteArray, timeoutMillis: Int): MonitoringHttpResponse
}

/** Android's public ID-only submit and exact-receipt status protocol. */
class HttpMonitoringProtocol(
    baseUrl: String,
    private val transport: MonitoringHttpTransport = UrlConnectionMonitoringTransport,
    private val clockMillis: () -> Long = System::currentTimeMillis,
    private val timeoutMillis: Int = DEFAULT_TIMEOUT_MILLIS,
) : MonitoringProtocol {
    private val origin: URI

    init {
        val parsed = runCatching { URI(baseUrl.trim().trimEnd('/')) }.getOrNull()
        require(parsed != null && parsed.scheme.equals("https", ignoreCase = true) &&
            !parsed.host.isNullOrBlank() && parsed.userInfo == null && parsed.query == null && parsed.fragment == null) {
            "Monitoring service must use an HTTPS origin"
        }
        require(timeoutMillis in 1..MAX_TIMEOUT_MILLIS) { "Invalid monitoring timeout" }
        origin = parsed
    }

    override suspend fun submit(tmdbId: Int): SubmissionReply = withContext(Dispatchers.IO) {
        requireValidId(tmdbId)
        val response = try {
            perform("v1/series-requests", "{\"tmdbId\":$tmdbId}".toByteArray(StandardCharsets.UTF_8))
        } catch (failure: MonitoringProtocolException) {
            if (failure.retryable) return@withContext SubmissionReply(accepted = false)
            throw failure
        }
        val retryAfter = retryAfterMillis(response.headers)
        when {
            response.statusCode == HTTP_ACCEPTED -> {
                val receipt = try {
                    parseSubmission(response.body, tmdbId)
                } catch (_: Exception) {
                    throw MonitoringProtocolException(retryable = false)
                }
                SubmissionReply(accepted = true, receipt = receipt)
            }
            isTransientStatus(response.statusCode) -> SubmissionReply(accepted = false, retryAfterMillis = retryAfter)
            else -> throw MonitoringProtocolException(retryable = false)
        }
    }

    override suspend fun status(tmdbId: Int, receipt: String): ReceiptReply = withContext(Dispatchers.IO) {
        requireValidId(tmdbId)
        requireValidReceipt(receipt)
        val payload = "{\"tmdbId\":$tmdbId,\"requestReceipt\":\"$receipt\"}".toByteArray(StandardCharsets.UTF_8)
        val response = try {
            perform("v1/series-requests/status", payload)
        } catch (failure: MonitoringProtocolException) {
            if (failure.retryable) return@withContext ReceiptReply(ReceiptState.TEMPORARY_FAILURE)
            throw failure
        }
        val retryAfter = retryAfterMillis(response.headers)
        when {
            response.statusCode == HTTP_OK -> {
                val state = try {
                    parseStatus(response.body, tmdbId)
                } catch (_: Exception) {
                    throw MonitoringProtocolException(retryable = false)
                }
                ReceiptReply(state)
            }
            isTransientStatus(response.statusCode) -> ReceiptReply(ReceiptState.TEMPORARY_FAILURE, retryAfter)
            else -> throw MonitoringProtocolException(retryable = false)
        }
    }

    private suspend fun perform(path: String, body: ByteArray): MonitoringHttpResponse {
        val url = URL(origin.toString().trimEnd('/') + "/" + path)
        return try {
            val result = transport.execute(url, body, timeoutMillis)
            if (result.body.size > MAX_RESPONSE_BYTES) throw MonitoringProtocolException(retryable = false)
            result
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (protocol: MonitoringProtocolException) {
            throw protocol
        } catch (_: Exception) {
            throw MonitoringProtocolException(retryable = true)
        }
    }

    private fun retryAfterMillis(headers: Map<String, List<String>>): Long? {
        val value = headers.entries.firstOrNull { it.key.equals("Retry-After", ignoreCase = true) }
            ?.value?.firstOrNull()?.trim() ?: return null
        val rawMillis = value.toLongOrNull()?.let { seconds ->
            if (seconds < 0) return null
            if (seconds > MAX_RETRY_AFTER_MILLIS / 1000L) MAX_RETRY_AFTER_MILLIS else seconds * 1000L
        } ?: try {
            val target = Instant.from(DateTimeFormatter.RFC_1123_DATE_TIME.parse(value)).toEpochMilli()
            (target - clockMillis()).coerceAtLeast(0L)
        } catch (_: DateTimeParseException) {
            return null
        } catch (_: ArithmeticException) {
            return null
        }
        return rawMillis.coerceIn(MIN_RETRY_AFTER_MILLIS, MAX_RETRY_AFTER_MILLIS)
    }

    private fun parseSubmission(bytes: ByteArray, expectedId: Int): String? = parseStrictObject(bytes) { reader, fields ->
        var accepted: Boolean? = null
        var tmdbId: Long? = null
        var state: String? = null
        var receipt: String? = null
        while (reader.hasNext()) {
            val name = reader.nextName()
            if (!fields.add(name)) invalidJson()
            when (name) {
                "accepted" -> {
                    if (reader.peek() != JsonToken.BOOLEAN) invalidJson()
                    accepted = reader.nextBoolean()
                }
                "tmdbId" -> tmdbId = readPositiveInteger(reader)
                "state" -> {
                    if (reader.peek() != JsonToken.STRING) invalidJson()
                    state = reader.nextString()
                }
                "requestReceipt" -> {
                    if (reader.peek() != JsonToken.STRING) invalidJson()
                    receipt = reader.nextString()
                }
                else -> invalidJson()
            }
        }
        reader.endObject()
        if (fields != setOf("accepted", "tmdbId", "state") && fields != setOf("accepted", "tmdbId", "state", "requestReceipt")) invalidJson()
        if (accepted != true || tmdbId != expectedId.toLong() || state !in setOf("queued", "already_queued")) invalidJson()
        if (receipt != null && !RECEIPT_PATTERN.matches(receipt!!)) invalidJson()
        receipt
    }

    private fun parseStatus(bytes: ByteArray, expectedId: Int): ReceiptState = parseStrictObject(bytes) { reader, fields ->
        var tmdbId: Long? = null
        var state: String? = null
        while (reader.hasNext()) {
            val name = reader.nextName()
            if (!fields.add(name)) invalidJson()
            when (name) {
                "tmdbId" -> tmdbId = readPositiveInteger(reader)
                "state" -> {
                    if (reader.peek() != JsonToken.STRING) invalidJson()
                    state = reader.nextString()
                }
                else -> invalidJson()
            }
        }
        reader.endObject()
        if (fields != setOf("tmdbId", "state") || tmdbId != expectedId.toLong()) invalidJson()
        when (state) {
            "pending" -> ReceiptState.PENDING
            "complete" -> ReceiptState.COMPLETE
            "unknown" -> ReceiptState.UNKNOWN
            else -> invalidJson()
        }
    }

    private fun <T> parseStrictObject(bytes: ByteArray, parse: (JsonReader, MutableSet<String>) -> T): T {
        if (bytes.size > MAX_RESPONSE_BYTES) invalidJson()
        val text = try {
            StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(bytes)).toString()
        } catch (_: Exception) {
            invalidJson()
        }
        val reader = JsonReader(StringReader(text)).apply { isLenient = false }
        val parsed = try {
            if (reader.peek() != JsonToken.BEGIN_OBJECT) invalidJson()
            reader.beginObject()
            parse(reader, mutableSetOf()).also {
                if (reader.peek() != JsonToken.END_DOCUMENT) invalidJson()
            }
        } catch (_: Exception) {
            invalidJson()
        } finally {
            runCatching { reader.close() }
        }
        return parsed
    }

    private fun readPositiveInteger(reader: JsonReader): Long {
        if (reader.peek() != JsonToken.NUMBER) invalidJson()
        val value = reader.nextString()
        if (!INTEGER_PATTERN.matches(value)) invalidJson()
        return value.toLongOrNull()?.takeIf { it in 1..Int.MAX_VALUE.toLong() } ?: invalidJson()
    }

    private fun requireValidId(id: Int) {
        if (id <= 0) throw MonitoringProtocolException(retryable = false)
    }

    private fun requireValidReceipt(receipt: String) {
        if (!RECEIPT_PATTERN.matches(receipt)) throw MonitoringProtocolException(retryable = false)
    }

    private fun invalidJson(): Nothing = throw IllegalArgumentException("Invalid response")

    private companion object {
        const val HTTP_OK = 200
        const val HTTP_ACCEPTED = 202
        const val DEFAULT_TIMEOUT_MILLIS = 8_000
        const val MAX_TIMEOUT_MILLIS = 8_000
        const val MAX_RESPONSE_BYTES = 8 * 1024
        const val MIN_RETRY_AFTER_MILLIS = 1_000L
        const val MAX_RETRY_AFTER_MILLIS = 6 * 60 * 60 * 1000L
        val INTEGER_PATTERN = Regex("0|[1-9][0-9]*")
        val RECEIPT_PATTERN = Regex("[0-9a-f]{64}")

        fun isTransientStatus(status: Int) = status == 408 || status == 429 || status in 500..599
    }
}

private object UrlConnectionMonitoringTransport : MonitoringHttpTransport {
    override fun execute(url: URL, body: ByteArray, timeoutMillis: Int): MonitoringHttpResponse {
        val connection = url.openConnection() as? HttpURLConnection ?: throw IOException()
        try {
            connection.instanceFollowRedirects = false
            connection.requestMethod = "POST"
            connection.connectTimeout = timeoutMillis
            connection.readTimeout = timeoutMillis
            connection.doOutput = true
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("Content-Type", "application/json")
            connection.setFixedLengthStreamingMode(body.size)
            connection.outputStream.use { it.write(body) }
            val status = connection.responseCode
            val stream = if (status in 200..399) connection.inputStream else connection.errorStream
            val responseBody = stream?.use { readBounded(it, 8 * 1024) } ?: byteArrayOf()
            return MonitoringHttpResponse(status, connection.headerFields, responseBody)
        } finally {
            connection.disconnect()
        }
    }

    private fun readBounded(input: java.io.InputStream, limit: Int): ByteArray {
        val output = ByteArrayOutputStream()
        val buffer = ByteArray(1024)
        while (true) {
            val count = input.read(buffer, 0, minOf(buffer.size, limit + 1 - output.size()))
            if (count < 0) break
            output.write(buffer, 0, count)
            if (output.size() > limit) throw MonitoringProtocolException(retryable = false)
        }
        return output.toByteArray()
    }
}
