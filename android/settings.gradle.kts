pluginManagement {
    repositories {
        google()
        mavenCentral()
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

rootProject.name = "meshlearn-android"

// core: the phone's logic in plain Kotlin (no Android), tested on the JVM.
// desktop: runs the phone on a computer with the simulated radio, for testing against the Python hub.
// app: the Android app (needs the Android SDK; skipped when no SDK is configured).
include(":core", ":desktop")
if (file("local.properties").exists() || System.getenv("ANDROID_HOME") != null) {
    include(":app")
}
