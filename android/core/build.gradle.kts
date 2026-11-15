plugins {
    id("org.jetbrains.kotlin.jvm")
    `java-library`
    id("com.android.lint")   // lets the app's lint check this code against the oldest supported Android
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

sourceSets {
    main {
        // The starter library pages ship inside the app, from the same files the Python phone uses.
        resources {
            srcDir("../../meshlearn")
            include("starter_library/**")
        }
    }
}

dependencies {
    api("org.nanohttpd:nanohttpd:2.3.1")
    compileOnly("org.xerial:sqlite-jdbc:3.50.3.0")   // JdbcDb, used by the desktop runner and tests only

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.xerial:sqlite-jdbc:3.50.3.0")
}

tasks.test {
    // The test vectors and the Python cross-check live next to the Python code.
    systemProperty("meshlearn.repo", rootProject.projectDir.parentFile.absolutePath)
}
