import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// The Gradle root is tv/, one level below the repo root, but versioning reads the whole
// project's git history so every installed build says exactly what it is (see the root
// AGENTS.md-style convention shared with the server). Run git against the repo root explicitly
// rather than relying on cwd.
val repoRoot = rootDir.parentFile
val gitCommitCount = providers.exec {
    workingDir = repoRoot
    commandLine("git", "-C", repoRoot.path, "rev-list", "--count", "HEAD")
}.standardOutput.asText.map { it.trim().toIntOrNull() ?: 1 }
val gitDirty = providers.exec {
    workingDir = repoRoot
    commandLine("git", "-C", repoRoot.path, "status", "--porcelain", "--untracked-files=no", "--", "tv")
}.standardOutput.asText.map { it.isNotBlank() }
// Where the version name comes from, in order of precedence.
//
// 1. An explicit CINEMATICA_VERSION (or -PcinematicaVersion). CI passes the release's
//    version in, so the APK inside a release says what the release says.
// 2. A release tag pointing at HEAD. Building the tagged commit by hand then produces the
//    same APK CI would, rather than something that merely looks similar.
// 3. The counted development version below.
//
// This order exists because of what shipped without it: a hand-built APK whose versionName
// was "0.109.17-dirty" was attached to the 1.0.0 release as Cinematica-TV-1.0.0.apk. The
// counted scheme cannot produce "1.0.0" -- it never will, the major is fixed at 0 -- so a
// build has to be TOLD the release version. Now it is, and a tagged build that disagrees
// with its tag is caught by tv-release.yml's versionName check instead of being published.
val releaseVersion = providers.environmentVariable("CINEMATICA_VERSION")
    .orElse(providers.gradleProperty("cinematicaVersion"))
    .map { it.trim().removePrefix("tv-").removePrefix("v") }
    .orElse("")
// Both spellings are accepted: the product ships one tag covering server, TV and the
// provider packages together (v1.0.0), and tv-release.yml also takes a TV-only tv-v* tag.
val releaseTagPattern = Regex("""^(?:tv-)?v(\d+\.\d+\.\d+)$""")
val gitTagVersion = providers.exec {
    workingDir = repoRoot
    // --points-at, not `describe`: only a tag on THIS commit is a release build. `describe`
    // would happily call the fortieth commit after v1.0.0 a 1.0.0 build.
    commandLine("git", "-C", repoRoot.path, "tag", "--points-at", "HEAD")
}.standardOutput.asText.map { out ->
    out.lines().firstNotNullOfOrNull { releaseTagPattern.find(it.trim())?.groupValues?.get(1) } ?: ""
}

// The development version is 0.<features>.<bugfixes>, both counted over the WHOLE repository's
// history (server commits included) so the number only ever goes up and says how much the
// product has grown. A commit is a bugfix when its subject reads like one; everything else
// counts as a feature. The major stays at 0: a counted version is not a released one, and the
// released number comes from the tag above.
val bugfixSubject = Regex(
    """^(fix|fixed|fixes|correct|corrects|stop|never|prevent|kill)\b|\bbugs?\b|\bdeadlock|\bregression|\breliably\b|\bproperly\b""",
    RegexOption.IGNORE_CASE
)
val countedVersion = providers.exec {
    workingDir = repoRoot
    commandLine("git", "-C", repoRoot.path, "log", "--format=%s")
}.standardOutput.asText.map { log ->
    val subjects = log.lines().filter { it.isNotBlank() }
    val fixes = subjects.count { bugfixSubject.containsMatchIn(it) }
    "0.${subjects.size - fixes}.$fixes"
}

// "-dirty" marks a build with uncommitted changes under tv/, and it is appended to a tagged
// version too -- deliberately. A release built from a modified tree is not that release, and
// saying so is what makes CI's tag-vs-versionName check able to reject it.
val gitDescribe = releaseVersion.zip(gitTagVersion) { explicit, tag ->
    if (explicit.isNotEmpty()) explicit else tag
}.zip(countedVersion) { released, counted ->
    if (released.isNotEmpty()) released else counted
}.zip(gitDirty) { version, dirty ->
    version + (if (dirty) "-dirty" else "")
}

// The release signing key. CI passes it through the environment; locally an optional
// signing.properties (gitignored) does the same job, so a build from this machine and a build
// from Actions install over each other instead of colliding. With neither, release stays
// debug-signed exactly as it always was.
val signingProps: Properties? =
    rootProject.file("signing.properties").takeIf { it.exists() }?.let { f ->
        val props = Properties()
        f.inputStream().use { stream -> props.load(stream) }
        props
    }
fun signingValue(env: String, prop: String): String? =
    providers.environmentVariable(env).orNull ?: signingProps?.getProperty(prop)

val keystorePath = signingValue("CINEMATICA_KEYSTORE_PATH", "storeFile")
val keystorePassword = signingValue("CINEMATICA_KEYSTORE_PASSWORD", "storePassword")
val keystoreAlias = signingValue("CINEMATICA_KEY_ALIAS", "keyAlias")
val keystoreKeyPassword = signingValue("CINEMATICA_KEY_PASSWORD", "keyPassword")

android {
    namespace = "io.github.superthom196.cinematica"
    compileSdk = 37

    defaultConfig {
        applicationId = "io.github.superthom196.cinematica"
        minSdk = 28
        targetSdk = 36
        // versionName is the release version when there is one -- CINEMATICA_VERSION, or a
        // v*/tv-v* tag on HEAD -- and otherwise the counted 0.<features>.<bugfixes>
        // development version, with "-dirty" appended when tv/ has uncommitted changes.
        // See gitDescribe above.
        //
        // versionCode stays the whole repo's commit count regardless. It is what Android
        // compares to decide one build is newer than another, so it has to keep rising
        // across releases AND across the development builds between them; a released
        // version number cannot do that job, and 1.0.1 would sort below the dev builds
        // that preceded it.
        versionCode = gitCommitCount.get()
        versionName = gitDescribe.get()

        // The Bravia (and every target TV) is 32-bit ARM; the libvlc-all AAR ships four ABIs and
        // is ~93 MB, so only the one actually needed is kept.
        ndk {
            abiFilters += "armeabi-v7a"
        }
    }

    signingConfigs {
        if (keystorePath != null) {
            create("release") {
                storeFile = file(keystorePath)
                storePassword = keystorePassword
                keyAlias = keystoreAlias
                keyPassword = keystoreKeyPassword
            }
        }
    }

    buildTypes {
        release {
            // Signed with the release key when one is configured, debug-signed otherwise; either way
            // it side-loads. Minified so it starts fast on a slow TV CPU.
            isMinifyEnabled = true
            isShrinkResources = true
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    lint {
        // lintVital crashes on this toolchain (missing IntelliJ mock classes); not needed for side-loading.
        checkReleaseBuilds = false
        abortOnError = false
    }

    packaging {
        // libvlc-all bundles the same native libs under multiple JNI dirs; first one wins.
        jniLibs.pickFirsts += "**/*.so"
    }
}

dependencies {
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.compose.foundation)
    implementation(libs.compose.material.icons)
    implementation(libs.tv.material)
    implementation(libs.activity.compose)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.lifecycle.viewmodel.compose)
    implementation(libs.core.ktx)
    implementation(libs.coil.compose)
    implementation(libs.coil.okhttp)
    implementation(libs.okhttp)
    implementation(libs.serialization.json)
    implementation(libs.coroutines.android)
    implementation(libs.datastore.preferences)
    implementation(libs.libvlc)
    testImplementation(libs.junit)
}
