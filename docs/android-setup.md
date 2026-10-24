# Setting up the community phone

> **Status:** these steps follow Termux's documented behaviour but have not yet been run end to end on a phone for this project. Note anything that differs and update this page.

The phone does three jobs:

- runs a Wi-Fi hotspot that students join;
- runs the MeshLearn student site with its data on the microSD card;
- talks to its LoRa radio, which has joined the same hotspot.

## 1. Install Termux

1. Install **F-Droid** from https://f-droid.org, then install **Termux** and **Termux:Boot** from F-Droid. The Google Play version of Termux is outdated and will not work.
2. Open Termux and run:
   ```bash
   pkg update
   pkg install python git
   termux-setup-storage        # allow access; creates ~/storage
   ```

## 2. Put MeshLearn on the phone

```bash
cd ~
git clone https://github.com/Yitz42/Global_Engineering_Group_4.git meshlearn-app
pip install meshtastic          # only needed for real radios
```

If you can't get internet on the phone, copy the folder over USB instead.

## 3. Use the microSD card for data

Android only lets Termux write to its own folder on the SD card. After `termux-setup-storage`, that folder is at `~/storage/external-1` (it maps to `/storage/XXXX-XXXX/Android/data/com.termux/files`).

```bash
mkdir -p ~/storage/external-1/meshlearn/library
```

To fill the library from a computer, put the card in a card reader. Then run the fetcher with `--dest <card>/Android/data/com.termux/files/meshlearn/library`.

**Warning:** uninstalling Termux deletes this folder, library included.

## 4. Turn on the hotspot and connect the radio

1. Turn on the phone's Wi-Fi hotspot. Set a password you can share with students.
2. Connect the radio to the hotspot, following [hardware.md](hardware.md), step 6.
3. Find the radio's IP address (on its screen or in the Meshtastic app), for example `192.168.43.20`.
4. Find the phone's own hotspot address, which students will type into their browsers:
   ```bash
   ip -4 addr | grep inet
   ```
   It is often `192.168.43.1`, but newer Android versions pick a different range.

## 5. Start the student site

```bash
cd ~/meshlearn-app
termux-wake-lock                # keep running with the screen off
python -m meshlearn phone \
  --data ~/storage/external-1/meshlearn \
  --hub '!a1b2c3d4' \
  --name "North village" \
  --radio-host 192.168.43.20
```

- `--hub` is the teacher hub radio's node id.
- Students open `http://<phone hotspot address>:8080`.
- `http://<address>:8080/status` shows how many messages are waiting for the radio and how much space is left on the card.

## 6. Load the assignments before the phone leaves school

Loading every assignment in advance means the radio later only carries answers, grades, and one-packet "open" messages. There are two ways to do it.

**Over school Wi-Fi**

1. Connect the phone to the same Wi-Fi as the teacher's laptop. The hub must be started with `--host 0.0.0.0`.
2. On the phone itself, open `http://localhost:8080/status`.
3. Enter the hub's address, for example `http://192.168.1.20:8000`, and press **Copy assignments**.

To do this from another device instead, start the phone with `--admin-pin 1234` and enter the PIN on the form. Without a PIN, only the phone's own browser can change what is stored.

**By memory card**

1. On the hub dashboard, press **Download course pack**.
2. Unzip it into the MeshLearn folder on the card, next to `library`. Alternatively, run `python3 -m meshlearn pack --out <that folder>` on the hub laptop.
3. The phone loads the pack when it starts. You can also press **Reload from card** on its status page.

Assignments stay hidden from students until the teacher opens them from the hub.

## 7. Open the big encyclopedias (.zim files)

Wikipedia and the other Kiwix collections are `.zim` files.

Install the **Kiwix** app on the phone and point it at the library folder. Kiwix can serve its books over the hotspot.

Pass its address so the Library page links straight to it:

```bash
--kiwix-url http://<phone hotspot address>:<port Kiwix shows>
```

If Kiwix and MeshLearn both want the same port, start MeshLearn with `--port 8081`.

## 8. Start automatically after a reboot

Create `~/.termux/boot/start-meshlearn`:

```bash
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd ~/meshlearn-app
python -m meshlearn phone --data ~/storage/external-1/meshlearn --hub '!a1b2c3d4' --name "North village" --radio-host 192.168.43.20 >> ~/meshlearn.log 2>&1
```

Then run `chmod +x ~/.termux/boot/start-meshlearn` and open the Termux:Boot app once.

**Battery:** in Android settings, set Termux to "unrestricted" (no battery optimisation) so Android does not stop it.
