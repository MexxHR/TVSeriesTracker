package com.example.tvseriestracker.data.remote

import com.example.tvseriestracker.BuildConfig
import com.google.gson.annotations.SerializedName
import java.io.IOException
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import retrofit2.http.GET
import retrofit2.http.Path
import retrofit2.http.Query

data class TvSearchResponse(val results: List<TvSeriesDto>?)

data class TvSeriesDto(
    val id: Int?,
    val name: String?,
    @SerializedName("original_name") val originalName: String?,
    @SerializedName("first_air_date") val firstAirDate: String?,
    val overview: String?,
    @SerializedName("poster_path") val posterPath: String?,
    @SerializedName("backdrop_path") val backdropPath: String?,
    val networks: List<TvNetworkDto>?
)

data class TvNetworkDto(val name: String?)

private interface TmdbApi {
    @GET("v1/tmdb/search")
    suspend fun search(
        @Query("query") query: String,
        @Query("language") language: String
    ): TvSearchResponse

    @GET("v1/tmdb/series/{id}")
    suspend fun details(
        @Path("id") id: Int,
        @Query("language") language: String
    ): TvSeriesDto
}

class MissingTmdbServiceException : IOException("TMDB metadata service is not configured")

interface SeriesRemoteDataSource {
    suspend fun search(query: String, language: String): List<TvSeriesDto>
    suspend fun details(tmdbId: Int, language: String): TvSeriesDto
}

class TmdbRemoteDataSource(private val baseUrl: String = BuildConfig.SERIES_REQUEST_API_BASE_URL) : SeriesRemoteDataSource {
    private val api: TmdbApi by lazy {
        Retrofit.Builder()
            .baseUrl(baseUrl.trimEnd('/') + "/")
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(TmdbApi::class.java)
    }

    private fun configured() {
        if (baseUrl.isBlank() || !baseUrl.startsWith("https://")) throw MissingTmdbServiceException()
    }

    override suspend fun search(query: String, language: String): List<TvSeriesDto> {
        configured()
        return api.search(query, language).results.orEmpty()
    }

    override suspend fun details(tmdbId: Int, language: String): TvSeriesDto {
        configured()
        return api.details(tmdbId, language)
    }
}
