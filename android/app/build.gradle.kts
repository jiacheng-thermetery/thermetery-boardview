import java.io.File

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// ---------------------------------------------------------------------------
// Python source staging (contract docs/android_contract.md §6).
//
// Chaquopy packages exactly these modules, copied from the repo root into a
// build-generated directory. NEVER srcDir the repo root itself — it contains
// a gitignored sample board and Windows DLLs that must not ship in the APK.
// ---------------------------------------------------------------------------
val pythonModules = listOf(
    "board_export.py",
    "boardview.py",
    "gencad_parser.py",
    "brd_parser.py",
    "tvw_parser.py",
    "tvw_master_fp.py",
    "tvw_compal.py",
    "tvw_topology.py",
    "tvw_seg_27_unified_v3.py",
    "fz_parser.py",
    "xzzpcb_parser.py",
    "ratsnest.py",
    "tvw_native.py",
    "xzz_native.py",
)

// android/ is the Gradle root project; the Python modules live one level up.
val repoRoot: File = rootProject.projectDir.parentFile
val stagedPythonDir = layout.buildDirectory.dir("staged-python")

val stagePythonSources = tasks.register<Copy>("stagePythonSources") {
    group = "build"
    description =
        "Copies the curated Python module list (contract §6) from the repo root " +
        "into the Chaquopy source dir."
    doFirst {
        val missing = pythonModules.filterNot { File(repoRoot, it).isFile }
        if (missing.isNotEmpty()) {
            throw GradleException(
                "stagePythonSources: missing module(s) in $repoRoot: " +
                missing.joinToString(", ") +
                " — the contract (docs/android_contract.md §6) requires all 14 " +
                "modules at the repo root. Do NOT edit the staging list; supply " +
                "the missing file(s) instead."
            )
        }
    }
    pythonModules.forEach { from(File(repoRoot, it)) }
    into(stagedPythonDir)
}

// Chaquopy's per-variant tasks consume the staged directory; make every task
// that touches Python sources (and preBuild, for belt and braces) run after
// the staging copy.
tasks.configureEach {
    if (name == "preBuild" ||
        (name != "stagePythonSources" && name.contains("Python"))
    ) {
        dependsOn(stagePythonSources)
    }
}

android {
    namespace = "com.thermetery.boardview"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.thermetery.boardview"
        minSdk = 24
        targetSdk = 35
        versionCode = 3
        versionName = "0.1.2"

        // Native kernels are prebuilt under src/main/jniLibs/<abi>/ for
        // exactly these two ABIs (16 KB-aligned, NDK r29).
        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    // Release signing is configured only when the keystore env vars are
    // present (BOARDVIEW_KEYSTORE / _KEYSTORE_PASSWORD / _KEY_ALIAS /
    // _KEY_PASSWORD). This keeps the keystore and its password out of the
    // repo; CI or a local build exports them. Without them, the release
    // variant stays unsigned (still useful for `bundletool`/manual signing).
    val ksPath = System.getenv("BOARDVIEW_KEYSTORE")
    val haveKeystore = ksPath != null && File(ksPath).isFile
    if (haveKeystore) {
        signingConfigs {
            create("release") {
                storeFile = File(ksPath)
                storePassword = System.getenv("BOARDVIEW_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("BOARDVIEW_KEY_ALIAS") ?: "boardview"
                keyPassword = System.getenv("BOARDVIEW_KEY_PASSWORD")
                    ?: System.getenv("BOARDVIEW_KEYSTORE_PASSWORD")
                enableV1Signing = true
                enableV2Signing = true
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
            if (haveKeystore) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

chaquopy {
    defaultConfig {
        version = "3.13"
        // Chaquopy 17 requires buildPython's minor version to MATCH the
        // target version above, so the anaconda 3.11 python named in the
        // contract cannot be used. CPython 3.13 was installed with the
        // Python install manager:  py install 3.13
        buildPython("C:/Users/Administrator/AppData/Local/Python/pythoncore-3.13-64/python.exe")
        pip {
            install("numpy==1.26.2")
        }
    }
    sourceSets {
        getByName("main") {
            srcDir(stagedPythonDir.get().asFile)
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
}
