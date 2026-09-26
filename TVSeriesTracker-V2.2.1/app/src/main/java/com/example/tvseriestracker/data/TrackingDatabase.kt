package com.example.tvseriestracker.data

import android.content.Context
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.Index
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.Transaction
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import kotlinx.coroutines.flow.Flow

@Entity(tableName = "tracked_series")
data class TrackedSeries(@PrimaryKey val id: String, val tmdbId: Int? = null)

@Entity(tableName = "series_metadata", indices = [Index(value = ["tmdbId"], unique = true)])
data class SeriesMetadataEntity(
    @PrimaryKey val id: String,
    val tmdbId: Int,
    val title: String,
    val originalTitle: String,
    val firstAirDate: String?,
    val year: Int?,
    val overview: String?,
    val posterPath: String?,
    val backdropPath: String?,
    val platform: String?
)

@Entity(tableName = "official_series_data")
data class OfficialDataEntity(
    @PrimaryKey val tmdbId: Int,
    val nextSeasonNumber: Int?,
    val status: String,
    val releaseDate: String?,
    val releaseYear: Int?,
    val sourceName: String,
    val sourceUrl: String,
    val announcementDate: String?,
    val lastChecked: String,
    val sourceType: String
)

@Entity(tableName = "official_sync_state")
data class OfficialSyncEntity(
    @PrimaryKey val id: Int = 1,
    val generatedAt: String,
    val lastSyncedAt: String
)

@Dao
interface TrackingDao {
    @Query("SELECT * FROM tracked_series") fun observeTracked(): Flow<List<TrackedSeries>>
    @Query("SELECT * FROM series_metadata") fun observeMetadata(): Flow<List<SeriesMetadataEntity>>
    @Query("SELECT * FROM series_metadata WHERE id = :id LIMIT 1") suspend fun metadataFor(id: String): SeriesMetadataEntity?
    @Insert(onConflict = OnConflictStrategy.IGNORE) suspend fun add(item: TrackedSeries)
    @Insert(onConflict = OnConflictStrategy.REPLACE) suspend fun saveMetadata(item: SeriesMetadataEntity)
    @Query("DELETE FROM tracked_series WHERE id = :id") suspend fun remove(id: String)
    @Query("SELECT * FROM official_series_data") fun observeOfficial(): Flow<List<OfficialDataEntity>>
    @Query("SELECT * FROM official_series_data") suspend fun officialRows(): List<OfficialDataEntity>
    @Query("SELECT * FROM official_sync_state WHERE id = 1") fun observeOfficialSync(): Flow<OfficialSyncEntity?>
    @Query("SELECT * FROM official_sync_state WHERE id = 1") suspend fun officialSync(): OfficialSyncEntity?
    @Query("DELETE FROM official_series_data") suspend fun clearOfficial()
    @Insert(onConflict = OnConflictStrategy.REPLACE) suspend fun saveOfficial(items: List<OfficialDataEntity>)
    @Insert(onConflict = OnConflictStrategy.REPLACE) suspend fun saveOfficialSync(state: OfficialSyncEntity)
    @Transaction suspend fun replaceOfficial(items: List<OfficialDataEntity>, state: OfficialSyncEntity) {
        clearOfficial()
        saveOfficial(items)
        saveOfficialSync(state)
    }
}

@Database(entities = [TrackedSeries::class, SeriesMetadataEntity::class, OfficialDataEntity::class, OfficialSyncEntity::class], version = 3, exportSchema = false)
abstract class TrackingDatabase : RoomDatabase() {
    abstract fun trackingDao(): TrackingDao

    companion object {
        private val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE tracked_series ADD COLUMN tmdbId INTEGER")
                DemoCatalog.tmdbIds.forEach { (id, tmdbId) ->
                    db.execSQL("UPDATE tracked_series SET tmdbId = ? WHERE id = ?", arrayOf(tmdbId, id))
                }
                db.execSQL("""CREATE TABLE IF NOT EXISTS series_metadata (
                    id TEXT NOT NULL PRIMARY KEY, tmdbId INTEGER NOT NULL, title TEXT NOT NULL,
                    originalTitle TEXT NOT NULL, firstAirDate TEXT, year INTEGER, overview TEXT,
                    posterPath TEXT, backdropPath TEXT, platform TEXT
                )""".trimIndent())
                db.execSQL("CREATE UNIQUE INDEX IF NOT EXISTS index_series_metadata_tmdbId ON series_metadata (tmdbId)")
            }
        }

        private val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("""CREATE TABLE IF NOT EXISTS official_series_data (
                    tmdbId INTEGER NOT NULL PRIMARY KEY, nextSeasonNumber INTEGER, status TEXT NOT NULL,
                    releaseDate TEXT, releaseYear INTEGER, sourceName TEXT NOT NULL, sourceUrl TEXT NOT NULL,
                    announcementDate TEXT, lastChecked TEXT NOT NULL, sourceType TEXT NOT NULL
                )""".trimIndent())
                db.execSQL("""CREATE TABLE IF NOT EXISTS official_sync_state (
                    id INTEGER NOT NULL PRIMARY KEY, generatedAt TEXT NOT NULL, lastSyncedAt TEXT NOT NULL
                )""".trimIndent())
            }
        }

        fun create(context: Context): TrackingDatabase = Room.databaseBuilder(
            context.applicationContext, TrackingDatabase::class.java, "series-tracking.db"
        ).addMigrations(MIGRATION_1_2, MIGRATION_2_3).addCallback(object : Callback() {
            override fun onCreate(db: SupportSQLiteDatabase) {
                super.onCreate(db)
                DemoCatalog.initialIds.forEach { id ->
                    db.execSQL("INSERT INTO tracked_series (id, tmdbId) VALUES (?, ?)", arrayOf(id, DemoCatalog.tmdbIds[id]))
                }
            }
        }).build()
    }
}
