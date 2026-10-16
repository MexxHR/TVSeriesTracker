package com.example.tvseriestracker
/** JVM-only type boundary. Network sources are injected fakes in every test. */
object BuildConfig {
 const val DEBUG = false
 const val OFFICIAL_DATA_URL = "https://example.com/synthetic-no-network"
 const val SERIES_REQUEST_API_BASE_URL = "https://example.invalid/synthetic-no-network"
}
