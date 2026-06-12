pluginManagement {
    repositories {
        google()
        mavenCentral()      // Chaquopy 17.x is published to Maven Central
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "thermetery-boardview"
include(":app")
