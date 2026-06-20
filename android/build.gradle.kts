// Top-level build file. Plugin versions live here; modules apply them
// without versions.
//
// Compatibility notes (see android/README.md):
//   Gradle 8.10.2 (local install, no wrapper)  <->  AGP 8.7.3  <->  Chaquopy 17.0.0
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("com.chaquo.python") version "17.0.0" apply false
}
