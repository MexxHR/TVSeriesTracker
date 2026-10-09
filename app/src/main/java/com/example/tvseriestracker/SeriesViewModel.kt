package com.example.tvseriestracker

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.example.tvseriestracker.data.AppLanguage
import com.example.tvseriestracker.data.AppTheme
import com.example.tvseriestracker.data.OfficialDataRepository
import com.example.tvseriestracker.data.Series
import com.example.tvseriestracker.data.SeriesRepository
import com.example.tvseriestracker.data.SettingsRepository
import com.example.tvseriestracker.data.trackAndQueueMonitoring
import com.example.tvseriestracker.data.remote.MissingTmdbServiceException
import com.example.tvseriestracker.data.remote.MonitoringRequestQueue
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import retrofit2.HttpException

enum class SearchPhase { PROMPT, LOADING, RESULTS, EMPTY, ERROR, RATE_LIMITED, SERVICE_UNAVAILABLE }
enum class OfficialRefreshPhase { IDLE, LOADING, SUCCESS, UP_TO_DATE, ERROR }
data class SearchUiState(val query: String = "", val phase: SearchPhase = SearchPhase.PROMPT, val results: List<Series> = emptyList())

internal fun searchFailurePhase(exception: Exception): SearchPhase = when {
    exception is MissingTmdbServiceException -> SearchPhase.SERVICE_UNAVAILABLE
    exception is HttpException && exception.code() == 429 -> SearchPhase.RATE_LIMITED
    else -> SearchPhase.ERROR
}

class SeriesViewModel(val series: SeriesRepository, val preferences: SettingsRepository,
    val official: OfficialDataRepository, private val monitoringQueue: MonitoringRequestQueue) : ViewModel() {
    private val _officialRefresh = MutableStateFlow(OfficialRefreshPhase.IDLE)
    val officialRefresh = _officialRefresh.asStateFlow()
    val officialCache = official.cache
    init { viewModelScope.launch {
        try { official.bootstrap(); official.refreshIfStale() }
        catch (exception: CancellationException) { throw exception }
        catch (_: Exception) { _officialRefresh.value = OfficialRefreshPhase.ERROR }
    } }
    private val _searchState = MutableStateFlow(SearchUiState())
    val searchState = _searchState.asStateFlow()
    private val _addingId = MutableStateFlow<String?>(null)
    val addingId = _addingId.asStateFlow()
    private val _addErrorId = MutableStateFlow<String?>(null)
    val addErrorId = _addErrorId.asStateFlow()
    private var searchJob: Job? = null

    fun updateQuery(query: String, language: AppLanguage) {
        searchJob?.cancel()
        if (query.trim().length < 2) {
            _searchState.value = SearchUiState(query = query)
            return
        }
        _searchState.value = SearchUiState(query = query, phase = SearchPhase.LOADING)
        searchJob = viewModelScope.launch {
            delay(400)
            executeSearch(query.trim(), language)
        }
    }

    fun retrySearch(language: AppLanguage) {
        val query = _searchState.value.query.trim()
        if (query.length < 2) return
        searchJob?.cancel()
        _searchState.value = _searchState.value.copy(phase = SearchPhase.LOADING)
        searchJob = viewModelScope.launch { executeSearch(query, language) }
    }

    private suspend fun executeSearch(query: String, language: AppLanguage) {
        try {
            val results = series.search(query, language)
            _searchState.value = SearchUiState(query, if (results.isEmpty()) SearchPhase.EMPTY else SearchPhase.RESULTS, results)
        } catch (exception: CancellationException) {
            throw exception
        } catch (exception: Exception) {
            _searchState.value = SearchUiState(query, searchFailurePhase(exception))
        }
    }

    fun track(item: Series, language: AppLanguage) {
        if (_addingId.value != null) return
        _addingId.value = item.id
        _addErrorId.value = null
        viewModelScope.launch {
            try {
                trackAndQueueMonitoring(series, monitoringQueue, item, language)
                _searchState.value = _searchState.value.copy(results = _searchState.value.results.map {
                    if (it.id == item.id) it.copy(isTracked = true) else it
                })
            } catch (exception: CancellationException) {
                throw exception
            } catch (_: Exception) {
                _addErrorId.value = item.id
            } finally {
                _addingId.value = null
            }
        }
    }

    fun untrack(id: String) { viewModelScope.launch { series.untrack(id) } }
    fun refreshMetadata(id: String, language: AppLanguage) { viewModelScope.launch {
        try { series.refreshMetadata(id, language) }
        catch (exception: CancellationException) { throw exception }
        catch (_: Exception) { /* Offline details keep the saved metadata. */ }
    } }
    fun setLanguage(language: AppLanguage) { viewModelScope.launch { preferences.setLanguage(language) } }
    fun setTheme(theme: AppTheme) { viewModelScope.launch { preferences.setTheme(theme) } }
    fun refreshOfficial() { viewModelScope.launch {
        _officialRefresh.value = OfficialRefreshPhase.LOADING
        try { _officialRefresh.value = if (official.refresh()) OfficialRefreshPhase.SUCCESS else OfficialRefreshPhase.UP_TO_DATE }
        catch (exception: CancellationException) { throw exception }
        catch (_: Exception) { _officialRefresh.value = OfficialRefreshPhase.ERROR }
    } }
}

class SeriesViewModelFactory(private val series: SeriesRepository, private val preferences: SettingsRepository,
    private val official: OfficialDataRepository, private val monitoringQueue: MonitoringRequestQueue) : ViewModelProvider.Factory {
    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>): T = SeriesViewModel(series, preferences, official, monitoringQueue) as T
}
