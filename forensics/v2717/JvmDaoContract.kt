/** JVM TEST TYPE DOUBLE; no Room engine or production DAO implementation. */
package com.example.tvseriestracker.data
import kotlinx.coroutines.flow.Flow

data class OfficialDataEntity(val tmdbId: Int, val nextSeasonNumber: Int?, val status: String,
 val releaseDate: String?, val releaseYear: Int?, val sourceName: String, val sourceUrl: String,
 val announcementDate: String?, val lastChecked: String, val sourceType: String)
data class OfficialSyncEntity(val id: Int = 1, val generatedAt: String, val lastSyncedAt: String)
interface TrackingDao {
 fun observeOfficial(): Flow<List<OfficialDataEntity>>
 fun observeOfficialSync(): Flow<OfficialSyncEntity?>
 suspend fun officialSync(): OfficialSyncEntity?
 suspend fun officialRows(): List<OfficialDataEntity>
 suspend fun replaceOfficial(items: List<OfficialDataEntity>, state: OfficialSyncEntity)
 suspend fun saveOfficialSync(state: OfficialSyncEntity)
}
