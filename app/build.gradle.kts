import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.kapt")
}

val localProperties = Properties().apply {
    val file = rootProject.file("local.properties")
    if (file.exists()) file.inputStream().use { load(it) }
}
val requestApiUrl = providers.gradleProperty("SERIES_REQUEST_API_BASE_URL").orNull?.takeIf(String::isNotBlank)
    ?: System.getenv("SERIES_REQUEST_API_BASE_URL")?.takeIf(String::isNotBlank)
    ?: localProperties.getProperty("SERIES_REQUEST_API_BASE_URL")?.takeIf(String::isNotBlank)
    ?: "https://mexxhr.pythonanywhere.com"
val encodedRequestApiUrl = "\"${requestApiUrl.replace("\\", "\\\\").replace("\"", "\\\"")}\""
val officialDataUrl = providers.gradleProperty("OFFICIAL_DATA_URL").orNull?.takeIf(String::isNotBlank)
    ?: System.getenv("OFFICIAL_DATA_URL")?.takeIf(String::isNotBlank)
    ?: localProperties.getProperty("OFFICIAL_DATA_URL")?.takeIf(String::isNotBlank)
    ?: "https://raw.githubusercontent.com/MexxHR/TVSeriesTracker/main/official-data/official_series_data.json"
val encodedOfficialDataUrl = "\"${officialDataUrl.replace("\\", "\\\\").replace("\"", "\\\"")}\""

android {
    namespace = "com.example.tvseriestracker"
    compileSdk = 35
    defaultConfig {
        applicationId = "com.example.tvseriestracker"
        minSdk = 26
        targetSdk = 35
        versionCode = 26
        versionName = "2.7.2"
        buildConfigField("String", "SERIES_REQUEST_API_BASE_URL", encodedRequestApiUrl)
        buildConfigField("String", "OFFICIAL_DATA_URL", encodedOfficialDataUrl)
    }
    buildTypes { release { isMinifyEnabled = false } }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true; buildConfig = true }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.12.01")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    debugImplementation("androidx.compose.ui:ui-tooling")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.navigation:navigation-compose:2.8.5")
    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    kapt("androidx.room:room-compiler:2.6.1")
    implementation("androidx.datastore:datastore-preferences:1.1.2")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("androidx.work:work-runtime-ktx:2.10.0")
    implementation("com.squareup.retrofit2:retrofit:2.11.0")
    implementation("com.squareup.retrofit2:converter-gson:2.11.0")
    implementation("io.coil-kt:coil-compose:2.7.0")
    implementation("io.coil-kt:coil-svg:2.7.0")
    testImplementation("junit:junit:4.13.2")
}
