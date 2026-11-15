plugins {
    id("com.android.application")
}

android {
    namespace = "org.meshlearn.phone"
    compileSdk = 37

    defaultConfig {
        applicationId = "org.meshlearn.phone"
        minSdk = 24          // Android 7.0: old phones donated for the pilot
        targetSdk = 37
        versionCode = 1
        versionName = "0.1.0"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    packaging {
        resources {
            excludes += setOf("META-INF/LICENSE*", "META-INF/NOTICE*")
        }
    }
}

dependencies {
    implementation(project(":core"))
}
