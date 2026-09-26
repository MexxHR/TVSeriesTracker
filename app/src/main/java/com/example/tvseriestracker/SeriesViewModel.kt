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
import com.example.tvseriestracker.data.remote.MissingTmdbTokenException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

enum class SearchPhase { PROMPT, LOADING, RESULTS, EMPTY, ERROR, MISSING_TOKEN }
enum class OfficialRefreshPhase { IDLE, LOADING, SUCCESS, ERROR }
data class SearchUiState(val query: String = "", val phase: SearchPhase = SearchPhase.PROMPT, val results: List<Series> = emptyList())

class SeriesViewModel(val series: SeriesRepository, val preferences: SettingsRepository,
    val official: OfficialDataRepository) : ViewModel() {
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
        } catch (_: MissingTmdbTokenException) {
            _searchState.value = SearchUiState(query, SearchPhase.MISSING_TOKEN)
        } catch (_: Exception) {
            _searchState.value = SearchUiState(query, SearchPhase.ERROR)
        }
    }

    fun track(item: Series, language: AppLanguage) {
        if (_addingId.value != null) return
        _addingId.value = item.id
        _addErrorId.value = null
        viewModelScope.launch {
            try {
                series.track(item, language)
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
        try { official.refresh(); _officialRefresh.value = OfficialRefreshPhase.SUCCESS }
        catch (exception: CancellationException) { throw exception }
        catch (_: Exception) { _officialRefresh.value = OfficialRefreshPhase.ERROR }
    } }
}

class SeriesViewModelFactory(private val series: SeriesRepository, private val preferences: SettingsRepository,
    private val official: OfficialDataRepository) : ViewModelProvider.Factory {
    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>): T = SeriesViewModel(series, preferences, official) as T
}
