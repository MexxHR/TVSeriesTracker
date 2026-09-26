package com.example.tvseriestracker

import android.content.Intent
import android.content.ActivityNotFoundException
import android.net.Uri
import android.os.Bundle
import androidx.activity.compose.setContent
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AppCompatDelegate
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.os.LocaleListCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.example.tvseriestracker.data.*
import com.example.tvseriestracker.data.remote.TmdbRemoteDataSource
import coil.compose.AsyncImage
import coil.decode.SvgDecoder
import coil.request.ImageRequest
import java.time.LocalDate
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlinx.coroutines.flow.collect

class MainActivity : AppCompatActivity() {
    private val database by lazy { TrackingDatabase.create(applicationContext) }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val official = OfficialDataRepository(RoomOfficialDataStore(database.trackingDao()),
            HttpOfficialRemoteSource(), bundledOfficialData(applicationContext))
        val factory = SeriesViewModelFactory(DefaultSeriesRepository(database.trackingDao(), TmdbRemoteDataSource(), official),
            SettingsRepository(applicationContext), official)
        setContent {
            val model: SeriesViewModel = viewModel(factory = factory)
            // Wait for DataStore before applying a locale. A temporary HR default here
            // would reverse a saved EN locale on every Activity recreation.
            val settings by produceState<AppSettings?>(initialValue = null, model) {
                model.preferences.settings.collect { value = it }
            }
            settings?.let { loadedSettings ->
                LaunchedEffect(loadedSettings.language) {
                    val tag = if (loadedSettings.language == AppLanguage.HR) "hr" else "en"
                    if (AppCompatDelegate.getApplicationLocales().toLanguageTags() != tag) {
                        AppCompatDelegate.setApplicationLocales(LocaleListCompat.forLanguageTags(tag))
                    }
                }
                val dark = when (loadedSettings.theme) {
                    AppTheme.SYSTEM -> androidx.compose.foundation.isSystemInDarkTheme()
                    AppTheme.LIGHT -> false
                    AppTheme.DARK -> true
                }
                MaterialTheme(colorScheme = if (dark) darkColorScheme(primary = Color(0xFF9BB9FF)) else lightColorScheme(primary = Color(0xFF355BAA))) {
                    TrackerApp(model, loadedSettings)
                }
            }
        }
    }
}

private data class Tab(val route: String, val label: Int, val icon: androidx.compose.ui.graphics.vector.ImageVector)
private val tabs = listOf(
    Tab("mine", R.string.tab_my_series, Icons.Default.Home),
    Tab("upcoming", R.string.tab_upcoming, Icons.Default.DateRange),
    Tab("add", R.string.tab_add, Icons.Default.Add),
    Tab("settings", R.string.tab_settings, Icons.Default.Settings)
)

@Composable
private fun TrackerApp(model: SeriesViewModel, settings: AppSettings) {
    val nav = rememberNavController()
    val entry by nav.currentBackStackEntryAsState()
    val route = entry?.destination?.route
    Scaffold(bottomBar = {
        if (route != "detail/{id}") NavigationBar {
            tabs.forEach { tab ->
                NavigationBarItem(
                    selected = route == tab.route,
                    onClick = { nav.navigate(tab.route) { popUpTo("mine") { saveState = true }; launchSingleTop = true; restoreState = true } },
                    icon = { Icon(tab.icon, contentDescription = null) },
                    label = { Text(stringResource(tab.label)) }
                )
            }
        }
    }) { padding ->
        NavHost(navController = nav, startDestination = "mine", modifier = Modifier.padding(padding)) {
            composable("mine") { MySeriesScreen(model) { nav.navigate("detail/$it") } }
            composable("upcoming") { UpcomingScreen(model, settings) { nav.navigate("detail/$it") } }
            composable("add") { AddScreen(model, settings) }
            composable("settings") { SettingsScreen(model, settings) }
            composable("detail/{id}", arguments = listOf(navArgument("id") { type = NavType.StringType })) { backStack ->
                val id = backStack.arguments?.getString("id").orEmpty()
                DetailScreen(model, id, settings, onBack = { nav.popBackStack() }, onRemoved = { nav.popBackStack() })
            }
        }
    }
}

private enum class WatchFilter { ALL, UPCOMING, RENEWED, WAITING, ENDING }

@Composable
private fun MySeriesScreen(model: SeriesViewModel, open: (String) -> Unit) {
    val series by model.series.tracked.collectAsStateWithLifecycle(initialValue = emptyList())
    var filter by remember { mutableStateOf(WatchFilter.ALL) }
    val filterLabels = listOf(R.string.filter_all, R.string.filter_upcoming, R.string.filter_renewed, R.string.filter_waiting, R.string.filter_ending)
    val visible = series.filter { item -> when (filter) {
        WatchFilter.ALL -> true
        WatchFilter.UPCOMING -> item.hasUpcomingOfficialDate(LocalDate.now())
        WatchFilter.RENEWED -> item.status == SeriesStatus.RENEWED || item.status == SeriesStatus.RELEASE_DATE_CONFIRMED
        WatchFilter.WAITING -> item.official == null
        WatchFilter.ENDING -> item.status == SeriesStatus.FINAL_SEASON
    } }
    Column {
        ScreenTitle(R.string.tab_my_series)
        LazyRow(contentPadding = PaddingValues(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            items(WatchFilter.entries) { option ->
                FilterChip(selected = filter == option, onClick = { filter = option }, label = { Text(stringResource(filterLabels[option.ordinal])) })
            }
        }
        if (visible.isEmpty()) EmptyState(R.string.no_tracked)
        else LazyColumn(contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            items(visible, key = { it.id }) { item -> SeriesCard(item) { open(item.id) } }
        }
    }
}

@Composable
private fun UpcomingScreen(model: SeriesViewModel, settings: AppSettings, open: (String) -> Unit) {
    val series by model.series.tracked.collectAsStateWithLifecycle(initialValue = emptyList())
    val dated = series.filter { it.hasUpcomingOfficialDate(LocalDate.now()) }.sortedBy { it.releaseDate }
    val undated = series.filter { it.official != null && it.releaseDate == null && it.status in setOf(SeriesStatus.RENEWED, SeriesStatus.FINAL_SEASON) }
    LazyColumn(contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item { Text(stringResource(R.string.tab_upcoming), style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold) }
        if (dated.isEmpty()) item { Text(stringResource(R.string.no_upcoming), color = MaterialTheme.colorScheme.onSurfaceVariant) }
        items(dated, key = { it.id }) { item ->
            val date = item.releaseDate!!
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(formatDate(date, settings.language), modifier = Modifier.width(92.dp), color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold)
                Box(Modifier.weight(1f)) { SeriesCard(item) { open(item.id) } }
            }
        }
        if (undated.isNotEmpty()) {
            item { Text(stringResource(R.string.undated_section), style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(top = 16.dp)) }
            items(undated, key = { it.id }) { item -> SeriesCard(item) { open(item.id) } }
        }
    }
}

@Composable
private fun AddScreen(model: SeriesViewModel, settings: AppSettings) {
    val search by model.searchState.collectAsStateWithLifecycle()
    val tracked by model.series.tracked.collectAsStateWithLifecycle(initialValue = emptyList())
    val addingId by model.addingId.collectAsStateWithLifecycle()
    val addErrorId by model.addErrorId.collectAsStateWithLifecycle()
    val trackedIds = tracked.map { it.id }.toSet()
    LaunchedEffect(settings.language) {
        if (search.query.trim().length >= 2) model.updateQuery(search.query, settings.language)
    }
    Column(Modifier.padding(horizontal = 16.dp)) {
        ScreenTitle(R.string.tab_add)
        OutlinedTextField(
            value = search.query,
            onValueChange = { model.updateQuery(it, settings.language) },
            label = { Text(stringResource(R.string.search_hint)) },
            singleLine = true,
            modifier = Modifier.fillMaxWidth()
        )
        Spacer(Modifier.height(12.dp))
        when (search.phase) {
            SearchPhase.PROMPT -> EmptyState(R.string.search_prompt)
            SearchPhase.LOADING -> Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
            SearchPhase.EMPTY -> EmptyState(R.string.no_search_results)
            SearchPhase.MISSING_TOKEN, SearchPhase.ERROR -> {
                Text(stringResource(if (search.phase == SearchPhase.MISSING_TOKEN) R.string.token_missing else R.string.search_error))
                Spacer(Modifier.height(8.dp))
                OutlinedButton(onClick = { model.retrySearch(settings.language) }) { Text(stringResource(R.string.retry)) }
            }
            SearchPhase.RESULTS -> LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp), contentPadding = PaddingValues(bottom = 16.dp)) {
                items(search.results, key = { it.id }) { item ->
                    Card {
                        Row(Modifier.fillMaxWidth().padding(12.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                            SeriesPoster(item.posterPath, Modifier.width(82.dp).height(124.dp))
                            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                Text(item.title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                                if (item.originalTitle != item.title) Text(item.originalTitle, style = MaterialTheme.typography.bodySmall)
                                Text(listOfNotNull(item.year?.let { stringResource(R.string.year_label, it) }, item.platform.takeIf { it.isNotBlank() }).joinToString(" • "), color = MaterialTheme.colorScheme.onSurfaceVariant)
                                item.overview?.let { Text(it, maxLines = 2, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodySmall) }
                                val alreadyTracked = item.id in trackedIds
                                Button(onClick = { model.track(item, settings.language) }, enabled = !alreadyTracked && addingId != item.id) {
                                    Text(stringResource(when {
                                        alreadyTracked -> R.string.following
                                        addingId == item.id -> R.string.adding_series
                                        else -> R.string.follow
                                    }))
                                }
                                if (addErrorId == item.id) Text(stringResource(R.string.add_error), color = MaterialTheme.colorScheme.error)
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun DetailScreen(model: SeriesViewModel, id: String, settings: AppSettings, onBack: () -> Unit, onRemoved: () -> Unit) {
    val series by remember(id) { model.series.observeSeries(id) }.collectAsStateWithLifecycle(initialValue = null)
    val context = LocalContext.current
    LaunchedEffect(id) { model.refreshMetadata(id, settings.language) }
    val item = series ?: return
    LazyColumn(contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        item { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = stringResource(R.string.back)) } }
        item { SeriesPoster(item.posterPath, Modifier.fillMaxWidth().height(210.dp)) }
        item { Text(item.title, style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold) }
        item { Text(stringResource(R.string.series_info), style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold) }
        if (item.originalTitle != item.title) item { InfoRow(stringResource(R.string.original_title), item.originalTitle) }
        item { InfoRow(stringResource(R.string.first_air_year), item.year?.toString() ?: stringResource(R.string.no_source)) }
        item { InfoRow(stringResource(R.string.overview), item.overview ?: stringResource(R.string.no_overview)) }
        item { InfoRow(stringResource(R.string.platform), item.platform.ifBlank { stringResource(R.string.platform_unavailable) }) }
        item { Text(stringResource(R.string.official_status), style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold, modifier = Modifier.padding(top = 8.dp)) }
        if (item.official == null) {
            item { Text(stringResource(R.string.no_verified_official_data), color = MaterialTheme.colorScheme.onSurfaceVariant) }
        } else {
            item { Text(stringResource(R.string.official_verified), color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold) }
            item { StatusBadge(item.status) }
            if (item.nextSeasonNumber != null) item { Text(stringResource(R.string.season_number, item.nextSeasonNumber)) }
            item {
                val release = when {
                    item.releaseDate != null -> formatDate(item.releaseDate, settings.language)
                    item.releaseYear != null -> item.releaseYear.toString()
                    else -> stringResource(R.string.date_unknown)
                }
                InfoRow(stringResource(if (item.releaseDate != null) R.string.release_date else R.string.release_year), release)
            }
            item { InfoRow(stringResource(R.string.official_source), item.officialSourceName.orEmpty()) }
            if (item.officialAnnouncementDate != null) item { InfoRow(stringResource(R.string.official_announcement), formatDate(item.officialAnnouncementDate, settings.language)) }
            item { InfoRow(stringResource(R.string.last_checked), formatDate(requireNotNull(item.lastChecked), settings.language)) }
            val sourceUrl = item.officialSourceUrl
            if (sourceUrl != null && OfficialDataParser.isSafeHttpsUrl(sourceUrl)) item {
                OutlinedButton(onClick = {
                    try { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(sourceUrl)).addCategory(Intent.CATEGORY_BROWSABLE)) }
                    catch (_: ActivityNotFoundException) { /* No browser on this device. */ }
                    catch (_: SecurityException) { /* Browser cannot be launched. */ }
                }) { Text(stringResource(R.string.open_official)) }
            }
        }
        if (item.isTracked) item {
            Button(onClick = { model.untrack(item.id); onRemoved() }, colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error)) { Text(stringResource(R.string.remove)) }
        }
    }
}

@Composable
private fun SettingsScreen(model: SeriesViewModel, settings: AppSettings) {
    val refresh by model.officialRefresh.collectAsStateWithLifecycle()
    val officialCache by model.officialCache.collectAsStateWithLifecycle(initialValue = OfficialCache(null, null, emptyList()))
    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(stringResource(R.string.tab_settings), style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
        Text(stringResource(R.string.language), style = MaterialTheme.typography.titleMedium)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(selected = settings.language == AppLanguage.HR, onClick = { model.setLanguage(AppLanguage.HR) }, label = { Text(stringResource(R.string.croatian)) })
            FilterChip(selected = settings.language == AppLanguage.EN, onClick = { model.setLanguage(AppLanguage.EN) }, label = { Text(stringResource(R.string.english)) })
        }
        Text(stringResource(R.string.theme), style = MaterialTheme.typography.titleMedium)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(selected = settings.theme == AppTheme.SYSTEM, onClick = { model.setTheme(AppTheme.SYSTEM) }, label = { Text(stringResource(R.string.theme_system)) })
            FilterChip(selected = settings.theme == AppTheme.LIGHT, onClick = { model.setTheme(AppTheme.LIGHT) }, label = { Text(stringResource(R.string.theme_light)) })
            FilterChip(selected = settings.theme == AppTheme.DARK, onClick = { model.setTheme(AppTheme.DARK) }, label = { Text(stringResource(R.string.theme_dark)) })
        }
        HorizontalDivider()
        Button(onClick = model::refreshOfficial, enabled = refresh != OfficialRefreshPhase.LOADING) {
            Text(stringResource(R.string.refresh_official_data))
        }
        if (refresh == OfficialRefreshPhase.LOADING) CircularProgressIndicator()
        if (refresh == OfficialRefreshPhase.SUCCESS) Text(stringResource(R.string.official_refresh_success))
        if (refresh == OfficialRefreshPhase.ERROR) Text(stringResource(R.string.official_refresh_error))
        officialCache.lastSyncedAt?.takeIf { it != java.time.Instant.EPOCH }?.let { synced ->
            Text(stringResource(R.string.official_last_update, formatDate(synced.atZone(java.time.ZoneId.systemDefault()).toLocalDate(), settings.language)))
        }
        HorizontalDivider()
        Text(stringResource(R.string.about), style = MaterialTheme.typography.titleMedium)
        val context = LocalContext.current
        AsyncImage(
            model = ImageRequest.Builder(context).data(R.raw.tmdb_logo).decoderFactory(SvgDecoder.Factory()).build(),
            contentDescription = stringResource(R.string.tmdb_logo_description),
            modifier = Modifier.width(190.dp).height(42.dp),
            contentScale = ContentScale.Fit
        )
        Text(stringResource(R.string.tmdb_attribution), style = MaterialTheme.typography.bodySmall)
    }
}

@Composable private fun ScreenTitle(id: Int) { Text(stringResource(id), style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold, modifier = Modifier.padding(16.dp)) }
@Composable private fun EmptyState(id: Int) { Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) { Text(stringResource(id), color = MaterialTheme.colorScheme.onSurfaceVariant) } }
@Composable private fun InfoRow(label: String, value: String) { Column { Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant); Text(value, style = MaterialTheme.typography.bodyLarge) } }

@Composable
private fun SeriesCard(item: Series, onClick: () -> Unit) {
    Card(onClick = onClick, shape = RoundedCornerShape(18.dp)) {
        Row(Modifier.fillMaxWidth().height(142.dp), verticalAlignment = Alignment.CenterVertically) {
            SeriesPoster(item.posterPath, Modifier.width(88.dp).fillMaxHeight())
            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                Text(item.title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold, maxLines = 1, overflow = TextOverflow.Ellipsis)
                if (item.nextSeasonNumber != null) Text(stringResource(R.string.season_number, item.nextSeasonNumber), style = MaterialTheme.typography.bodySmall)
                StatusBadge(item.status)
                Text(item.platform.ifBlank { stringResource(R.string.platform_unavailable) }, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (item.releaseDate != null) Text(item.releaseDate.toString(), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.primary)
            }
        }
    }
}

@Composable
private fun SeriesPoster(posterPath: String?, modifier: Modifier = Modifier) {
    Box(modifier.background(MaterialTheme.colorScheme.surfaceVariant), contentAlignment = Alignment.Center) {
        Text(stringResource(R.string.no_poster_available), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(6.dp))
        if (!posterPath.isNullOrBlank()) {
            AsyncImage(
                model = ImageRequest.Builder(LocalContext.current)
                    .data("https://image.tmdb.org/t/p/w342$posterPath")
                    .crossfade(true)
                    .build(),
                contentDescription = stringResource(R.string.poster_placeholder),
                modifier = Modifier.matchParentSize(),
                contentScale = ContentScale.Crop
            )
        }
    }
}

@Composable
private fun StatusBadge(status: SeriesStatus?) {
    val (label, color) = when (status) {
        SeriesStatus.RENEWED -> R.string.status_renewed to Color(0xFF267A47)
        SeriesStatus.RELEASE_DATE_CONFIRMED -> R.string.status_date to Color(0xFF3666BB)
        SeriesStatus.FINAL_SEASON -> R.string.status_final to Color(0xFF7851A9)
        SeriesStatus.CANCELED -> R.string.status_canceled to Color(0xFFB34242)
        null -> R.string.status_none to Color(0xFF666A73)
    }
    Box(Modifier.background(color, RoundedCornerShape(50)).padding(horizontal = 9.dp, vertical = 4.dp)) {
        Text(stringResource(label), color = Color.White, style = MaterialTheme.typography.labelSmall, maxLines = 1)
    }
}

private fun formatDate(date: LocalDate, language: AppLanguage): String =
    date.format(if (language == AppLanguage.HR) DateTimeFormatter.ofPattern("dd.MM.yyyy.", Locale.forLanguageTag("hr"))
        else DateTimeFormatter.ofPattern("d MMM yyyy", Locale.ENGLISH))
