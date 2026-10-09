package com.example.tvseriestracker

import com.example.tvseriestracker.data.remote.MissingTmdbServiceException
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Test
import retrofit2.HttpException
import retrofit2.Response

class SearchFailurePhaseTest {
    @Test fun rateLimitHasItsOwnUiPhase() {
        val response = Response.error<Any>(429, byteArrayOf().toResponseBody())
        assertEquals(SearchPhase.RATE_LIMITED, searchFailurePhase(HttpException(response)))
    }

    @Test fun otherFailuresKeepTheirExistingUiPhases() {
        val serverError = HttpException(Response.error<Any>(503, byteArrayOf().toResponseBody()))
        assertEquals(SearchPhase.ERROR, searchFailurePhase(serverError))
        assertEquals(SearchPhase.SERVICE_UNAVAILABLE, searchFailurePhase(MissingTmdbServiceException()))
        assertEquals(SearchPhase.ERROR, searchFailurePhase(IllegalStateException("offline")))
    }
}
