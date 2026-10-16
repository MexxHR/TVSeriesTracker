package com.example.tvseriestracker.data.remote
/** Queue transport boundary only; no WorkManager engine is simulated here. */
interface MonitoringRequestQueue { fun enqueue(tmdbId: Int); suspend fun ensure(tmdbId: Int) { enqueue(tmdbId) } }
