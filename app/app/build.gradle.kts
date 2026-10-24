import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.hilt)
    alias(libs.plugins.ksp)
}

val segredosLocais = Properties().apply {
    val arquivo = rootProject.file("local.properties")
    if (arquivo.exists()) arquivo.inputStream().use { load(it) }
}

fun local(nome: String): String =
    (segredosLocais.getProperty(nome) ?: System.getenv(nome) ?: "").trim()

android {
    namespace = "com.resonance.music"
    compileSdk = 35

    defaultConfig {
        applicationId = "eco.c.player"
        minSdk = 31
        targetSdk = 34
        versionCode = 1
        versionName = "1.0-eko"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        buildConfigField(
            "String",
            "GOOGLE_WEB_CLIENT_ID",
            "\"${local("GOOGLE_WEB_CLIENT_ID")}\""
        )
        buildConfigField(
            "String",
            "EKO_SERVER_URL",
            "\"${local("EKO_SERVER_URL").ifBlank { "http://10.0.2.2:8787" }}\""
        )
    }

    // From-source builds (e.g. F-Droid) have no signing material; only wire up a
    // release signing config when it's actually provided â€” via a local
    // signing.properties or CI environment variables. Otherwise the release build
    // stays unsigned and still compiles, so it can be built from source and signed
    // downstream.
    val hasReleaseSigning = rootProject.file("signing.properties").exists() ||
        System.getenv("SIGNING_KEYSTORE") != null

    signingConfigs {
        if (hasReleaseSigning) {
            create("release") {
                val props = rootProject.file("signing.properties")
                if (props.exists()) {
                    val signingProps = Properties()
                    props.inputStream().use { signingProps.load(it) }
                    storeFile = rootProject.file(signingProps["storeFile"] as String)
                    storePassword = signingProps["storePassword"] as String
                    keyAlias = signingProps["keyAlias"] as String
                    keyPassword = signingProps["keyPassword"] as String
                } else {
                    // CI: keystore path + secrets from environment variables.
                    storeFile = rootProject.file(System.getenv("SIGNING_KEYSTORE"))
                    storePassword = System.getenv("SIGNING_STORE_PASSWORD") ?: ""
                    keyAlias = System.getenv("SIGNING_KEY_ALIAS") ?: "resonance"
                    keyPassword = System.getenv("SIGNING_KEY_PASSWORD") ?: ""
                }
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            signingConfig = if (hasReleaseSigning) signingConfigs.getByName("release") else null
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    // F-Droid reproducibility: omit AGP's encrypted dependency-metadata block
    // (DependencyInfoBlock) from the signed APK.
    dependenciesInfo {
        includeInApk = false
        includeInBundle = false
    }
}

// F-Droid reproducibility: the ART/baseline profile (assets/dexopt/baseline.prof)
// that AGP compiles from merged library profiles is not byte-for-byte
// reproducible, so disable its generation.
// See https://f-droid.org/docs/Reproducible_Builds/
tasks.configureEach {
    if (name.contains("ArtProfile") || name.contains("StartupProfile")) {
        enabled = false
    }
}

dependencies {
    // Compose
    val composeBom = platform(libs.compose.bom)
    implementation(composeBom)
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.graphics)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.compose.material3)
    implementation(libs.compose.material.icons)
    implementation(libs.compose.animation)
    debugImplementation(libs.compose.ui.tooling)

    // AndroidX
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime)
    implementation(libs.androidx.lifecycle.viewmodel)
    implementation(libs.androidx.navigation.compose)
    implementation(libs.androidx.datastore)
    implementation(libs.androidx.splashscreen)

    // Hilt
    implementation(libs.hilt.android)
    ksp(libs.hilt.compiler)
    implementation(libs.hilt.navigation.compose)

    // Media3
    implementation(libs.media3.exoplayer)
    implementation(libs.media3.session)
    implementation(libs.media3.ui)

    // Network
    implementation(libs.retrofit)
    implementation(libs.retrofit.converter.gson)
    implementation(libs.okhttp)
    implementation(libs.okhttp.logging)

    // Login Google (mesmo fluxo do EKO)
    implementation("androidx.credentials:credentials:1.6.0")
    implementation("androidx.credentials:credentials-play-services-auth:1.6.0")
    implementation("com.google.android.libraries.identity.googleid:googleid:1.1.1")

    // Image loading
    implementation(libs.coil.compose)

    // Coroutines
    implementation(libs.kotlinx.coroutines)

    // Reorderable queue (Compose 1.6-compatible line)
    implementation(libs.reorderable)

    // Unit tests (JVM) â€” queue transform logic
    testImplementation(libs.junit)
}

