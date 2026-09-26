package com.example.tvseriestracker.data.remote

import com.example.tvseriestracker.BuildConfig
import com.google.gson.annotations.SerializedName
import java.io.IOException
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import retrofit2.http.GET
import retrofit2.http.Header
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
    @GET("search/tv")
    suspend fun search(
        @Header("Authorization") authorization: String,
        @Query("query") query: String,
        @Query("language") language: String,
        @Query("include_adult") includeAdult: Boolean = false
    ): TvSearchResponse

    @GET("tv/{id}")
    suspend fun details(
        @Header("Authorization") authorization: String,
        @Path("id") id: Int,
        @Query("language") language: String
    ): TvSeriesDto
}

class MissingTmdbTokenException : IOException("TMDB_API_TOKEN is not configured")

interface SeriesRemoteDataSource {
    suspend fun search(query: String, language: String): List<TvSeriesDto>
    suspend fun details(tmdbId: Int, language: String): TvSeriesDto
}

class TmdbRemoteDataSource(private val token: String = BuildConfig.TMDB_API_TOKEN) : SeriesRemoteDataSource {
    private val api: TmdbApi by lazy {
        Retrofit.Builder()
            .baseUrl("https://api.themoviedb.org/3/")
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(TmdbApi::class.java)
    }

    private fun authorization(): String {
        if (token.isBlank()) throw MissingTmdbTokenException()
        return "Bearer $token"
    }

    override suspend fun search(query: String, language: String): List<TvSeriesDto> =
        api.search(authorization(), query, language).results.orEmpty()

    override suspend fun details(tmdbId: Int, language: String): TvSeriesDto =
        api.details(authorization(), tmdbId, language)
}
