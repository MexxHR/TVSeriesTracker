package com.example.tvseriestracker.data

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.settingsStore by preferencesDataStore("settings")

enum class AppLanguage { HR, EN }
enum class AppTheme { SYSTEM, LIGHT, DARK }
data class AppSettings(val language: AppLanguage = AppLanguage.HR, val theme: AppTheme = AppTheme.SYSTEM)

class SettingsRepository(private val context: Context) {
    private val languageKey = stringPreferencesKey("language")
    private val themeKey = stringPreferencesKey("theme")
    val settings: Flow<AppSettings> = context.settingsStore.data.map { prefs ->
        AppSettings(
            runCatching { AppLanguage.valueOf(prefs[languageKey] ?: "HR") }.getOrDefault(AppLanguage.HR),
            runCatching { AppTheme.valueOf(prefs[themeKey] ?: "SYSTEM") }.getOrDefault(AppTheme.SYSTEM)
        )
    }
    suspend fun setLanguage(value: AppLanguage) { context.settingsStore.edit { it[languageKey] = value.name } }
    suspend fun setTheme(value: AppTheme) { context.settingsStore.edit { it[themeKey] = value.name } }
}
