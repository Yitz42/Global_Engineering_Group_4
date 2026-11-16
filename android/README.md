# MeshLearn phone for Android

The student phone as a real Android app, in Kotlin. It does what `python -m meshlearn phone`
does in Termux, and talks to the same Python teacher hub, but it can also:

- reach its LoRa radio over **Bluetooth** directly (Python's Bluetooth library does not run on Android),
  or over Wi-Fi if the radio is on the hotspot;
- keep running all day with the screen off (a foreground service), and start again after a reboot;
- keep its data on the microSD card or the phone's own storage, without Termux
  (Android/data/org.meshlearn.phone/files/meshlearn on either). It uses the card when there is one;
  the setup screen can switch, moving students' work and lessons (and the books, if there is room)
  and keeping the old databases as a backup.

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

1. Install the app and open it. Setup goes one step per screen: the phone's name, finding the radio
   over Bluetooth (or a Wi-Fi radio, or none yet), the school's radio id (`!a1b2c3d4`) and private
   channel, then a check that starts MeshLearn and shows what still needs doing (usually the hotspot).
2. If Android asks for a PIN while pairing, it is on the radio's screen (123456 on radios without one).
3. The last screen shows students how to join. After that the app opens on a status screen: whether
   everything works, the radio, when the school last answered, what is waiting, and what to try when
   the radio is lost. Settings (the gear) changes any step, where files are kept, and the technical options.
4. Add lessons from **Add lessons and books**: from the teacher's laptop over school Wi-Fi, or a course
   pack on the memory card (or copied over USB).

## Tested so far

- `core`: unit tests, and the end-to-end test against the Python hub (20% simulated packet loss).
- The app on the Android 16 emulator (API 37): starts the service, serves the student site, copies
  assignments from a Python hub, keeps its data on the (emulated) memory card, and the setup screen
  and in-app browser lay out correctly edge to edge.

- Android 7.0 (API 24), on an emulator sized like an old phone (1 GB RAM, 720×1280, memory card):
  the same checks pass, including building and queueing a compressed radio message. Every platform call
  newer than API 24 sits behind a version check, and lint checks `core` too (`checkDependencies`).
  This found one real crash on Android 7: `java.util.Base64` (Android 8+) in the shared dictionary,
  now hex.

## Not yet tested

- On a real Android phone and with real radios over Bluetooth: the Bluetooth code follows Meshtastic's
  documented BLE API but has not talked to a radio yet. The Wi-Fi radio option is the fallback.
- Real old phones: makers' battery savers that stop background apps (set the app to "unrestricted"),
  slow memory cards, and Bluetooth on old chipsets. Android 7.0 is the oldest version supported.
