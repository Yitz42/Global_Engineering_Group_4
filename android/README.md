# MeshLearn phone for Android

The student phone as a real Android app, in Kotlin. It does what `python -m meshlearn phone`
does in Termux, and talks to the same Python teacher hub, but it can also:

- reach its LoRa radio over **Bluetooth** directly (Python's Bluetooth library does not run on Android),
  or over Wi-Fi if the radio is on the hotspot;
- keep running all day with the screen off (a foreground service), and start again after a reboot;
- keep its data on the microSD card without Termux (Android/data/org.meshlearn.phone/files/meshlearn).

Students still use any browser: the phone serves the same pages on its hotspot.

## Layout

| Module | What it is |
|---|---|
| `core/` | The phone's logic in plain Kotlin: radio message format, store-and-forward messenger, assignments, the student site, Meshtastic protobufs. No Android code, so it is tested on a computer. |
| `desktop/` | Runs `core` on a computer with the simulated radio, to test against the Python hub. |
| `app/` | The Android app: Bluetooth radio link, background service, setup screen, in-app browser. |
| `tools/` | `gen_kotlin_shared.py` (shared dictionary and test vectors from the Python code), `e2e_python_hub.py` (end-to-end test). |

## Build and test

Needs Android Studio (its bundled Java works for everything below). Open this `android` folder
in Android Studio, or use the command line:

```bash
gradlew :core:test                          # unit tests, incl. frames made by the Python code
gradlew :desktop:installDist
python tools/e2e_python_hub.py --loss 0.2   # Python hub + Kotlin phone over the simulated radio
gradlew :app:assembleDebug                  # the APK (needs the Android SDK: local.properties or ANDROID_HOME)
```

Install on a phone with USB debugging on: `adb install app/build/outputs/apk/debug/app-debug.apk`,
or press Run in Android Studio.

If `protocol.py` or `assignments.py` changes, run `python tools/gen_kotlin_shared.py` and the tests again.
Install `meshtastic` first (as in `requirements-radio.txt`) to include its protobuf vectors.

## Setting up a phone

1. Install the app, open it, and fill in **Setup**: the phone's name, the teacher hub's radio id
   (`!a1b2c3d4`), the private channel index.
2. Pair the radio in Android's Bluetooth settings (PIN on its screen), then **Find my radio** and **Save**.
3. Press **Start** and turn on the phone's Wi-Fi hotspot. The screen shows the address students open.
4. Load assignments from **Phone status** (over school Wi-Fi from the hub, or a course pack on the card).

## Tested so far

- `core`: unit tests, and the end-to-end test against the Python hub (20% simulated packet loss).
- The app on the Android 16 emulator (API 37): starts the service, serves the student site, copies
  assignments from a Python hub, keeps its data on the (emulated) memory card, and the setup screen
  and in-app browser lay out correctly edge to edge.

- Android 7 compatibility, checked statically: every platform call newer than Android 7.0 (API 24)
  sits behind a version check, and lint checks `core` too (`checkDependencies`). This caught one real
  crash: `java.util.Base64` (Android 8+) in the shared dictionary, now hex.

## Not yet tested

- On a real Android phone and with real radios over Bluetooth: the Bluetooth code follows Meshtastic's
  documented BLE API but has not talked to a radio yet. The Wi-Fi radio option is the fallback.
- Android 7 phones specifically (the app targets Android 7.0 and newer).
