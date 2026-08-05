import java.io.File

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// ---------------------------------------------------------------------------
// Python source staging (contract docs/android_contract.md §6).
//
// After the upstream src-layout restructure (PR #3) the parser core lives in
// the `src.parsers` package and uses RELATIVE imports, so it can no longer be
// flattened into top-level modules. We stage two groups, preserving paths:
//   * board_export.py  -> staged root        (Kotlin loads getModule("board_export"))
//   * src/__init__.py, src/ratsnest.py, src/parsers/**  -> as a package,
//     so `import src.parsers.boardview` (and its `from .gencad_parser` …) resolve.
//
// NEVER srcDir the repo root itself — it holds gitignored sample boards and
// Windows DLLs that must not ship in the APK; we copy only this curated list.
// ---------------------------------------------------------------------------

// Top-level entry module — Chaquopy resolves Python.getModule("board_export").
val pythonRootModules = listOf(
    "board_export.py",
)

// The src.parsers package + its src-level dependency (ratsnest), staged with
// their repo-relative paths intact so the relative imports inside resolve.
val pythonPackageModules = listOf(
    "src/__init__.py",
    "src/ratsnest.py",
    "src/runtime_paths.py",
    // board_export imports the shared units_per_mm heuristic.
    "src/units.py",
    // board_export.validate_key delegates to src.key_store (shared with the
    // desktop key manager); it must ship or the key screen crashes on validate.
    "src/key_store.py",
    // tvw_compal & tvw_topology landed at src/ (not src/parsers/) in the restructure.
    "src/tvw_compal.py",
    "src/tvw_topology.py",
    "src/parsers/__init__.py",
    "src/parsers/boardview.py",
    "src/parsers/gencad_parser.py",
    "src/parsers/brd_parser.py",
    "src/parsers/asc_parser.py",
    "src/parsers/tvw_parser.py",
    "src/parsers/tvw_master_fp.py",
    "src/parsers/tvw_trace_scanners.py",
    "src/parsers/fz_parser.py",
    "src/parsers/xzzpcb_parser.py",
    "src/parsers/tvw_native.py",
    "src/parsers/xzz_native.py",
)

// android/ is the Gradle root project; the Python modules live one level up.
val repoRoot: File = rootProject.projectDir.parentFile
val stagedPythonDir = layout.buildDirectory.dir("staged-python")

val stagePythonSources = tasks.register<Copy>("stagePythonSources") {
    group = "build"
    description =
        "Copies the curated Python module list (contract §6) from the repo root " +
        "into the Chaquopy source dir, preserving the src.parsers package layout."
    val allModules = pythonRootModules + pythonPackageModules
    doFirst {
        val missing = allModules.filterNot { File(repoRoot, it).isFile }
        if (missing.isNotEmpty()) {
            throw GradleException(
                "stagePythonSources: missing module(s) in $repoRoot: " +
                missing.joinToString(", ") +
                " — after the src-layout restructure the parser core lives under " +
                "src/parsers/. Supply the missing file(s); do NOT flatten the list."
            )
        }
    }
    pythonRootModules.forEach { from(File(repoRoot, it)) }
    pythonPackageModules.forEach { rel ->
        val destDir = rel.substringBeforeLast('/', "")
        from(File(repoRoot, rel)) {
            if (destDir.isNotEmpty()) into(destDir)
        }
    }
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
        versionCode = 4
        versionName = "1.0.0"

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
