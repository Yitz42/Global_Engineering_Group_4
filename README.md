# MeshLearn

**Offline assignments and a local library for students in broadband deserts, linked by LoRa radio.**

ECEP 405 Global Engineering, Team 4 (Accessible Education), Fall 2026 prototype.

A teacher builds interactive assignments on a **teacher hub**. An **old Android phone** in the community keeps a copy of every assignment, plus an offline library, on its microSD card. It also runs a small website on its own Wi-Fi hotspot. The copies are loaded by memory card or over school Wi-Fi.

When the teacher opens an assignment, the hub sends a one-packet message over a Meshtastic LoRa mesh. Students join the hotspot with any device, read the library, and complete the work. The phone checks that answers are complete, batches several students' work together, and radios it back to the hub whenever the link is up. The teacher grades, and the results come back the same way.

The radio only carries a full assignment when the teacher adds or edits one after the phone's copy was loaded. No internet, cell signal, or ISP is needed at any point.

```mermaid
flowchart LR
  subgraph School["School (teacher)"]
    T[Teacher laptop<br/>MeshLearn hub<br/>dashboard + grading]
    R1((LoRa node<br/>USB))
    T --- R1
  end
  R1 <-. "LoRa mesh, several km<br/>(relays extend range)" .-> RL((Solar relay<br/>optional))
  RL <-. LoRa .-> R2
  subgraph Home["Community or household"]
    R2((LoRa node<br/>Wi-Fi))
    P[Old Android phone<br/>microSD: library + assignments<br/>runs hotspot + MeshLearn site]
    S1[Student phone]
    S2[Student tablet]
    S3[Shared laptop]
    R2 --- P
    P -- Wi-Fi hotspot --- S1
    P -- Wi-Fi hotspot --- S2
    P -- Wi-Fi hotspot --- S3
  end
```

| Piece | What it does | Runs on |
| --- | --- | --- |
| Teacher hub | Assignment builder and editor, open/hide per phone, course pack export, auto-grading, manual grading of written answers, CSV export | Any laptop with Python 3.9+ |
| Student phone | Stored assignments, student website with answer checking, results, offline library, batched store-and-forward queue | Old Android phone with Termux, data on microSD |
| Mesh link | Compressed, chunked, acknowledged messages that resend only lost pieces and survive reboots | Meshtastic radios, or a built-in simulator |
| Library fetcher | Picks the best open content that fits the card (Kiwix offline Wikipedia, Wikibooks, Wikiversity, PhET, Project Gutenberg) in English, Portuguese, or German, and downloads it with resume | Any computer with internet, before deployment |

## Try it in two minutes (no hardware)

Needs Python 3.9 or newer and nothing else.

```bash
git clone https://github.com/Yitz42/Global_Engineering_Group_4.git
cd Global_Engineering_Group_4
python3 -m meshlearn demo
```

Then open:

- **Student phone:** http://localhost:8080. Enter a name, open "Fractions and plants check-in", answer it, and submit.
- **Teacher hub:** http://localhost:8000. Watch the submission arrive a few seconds later, grade the written answer, and save. The student's "My results" page updates over the radio.

On start, the demo plays out a school visit. The phone copies the assignment from the hub over "Wi-Fi", then the hub opens it with a single radio packet. The phone's status page at http://localhost:8080/status counts the radio packets sent.

The simulated radio drops 20% of packets, so you can watch the retries work. Change that with `--loss 0.5`.

## Running on real hardware

1. **Get radios.** See [docs/hardware.md](docs/hardware.md) for recommended boards, antennas, power, and the right frequency band for the US, Brazil, Germany, and Kenya.
2. **Flash Meshtastic** on each radio (https://flasher.meshtastic.org), set the region, and put all radios on the same channel.
3. **Teacher hub:** plug a radio into the laptop by USB and run:
   ```bash
   pip install meshtastic
   python3 -m meshlearn hub --radio-serial /dev/ttyUSB0      # Windows: --radio-serial COM3
   ```
   Open http://localhost:8000. On the **Phones** page, add each community phone by its radio node id (for example `!a1b2c3d4`). Phones also register themselves the first time they check in.
   Add `--host 0.0.0.0` if phones will copy assignments from this laptop over school Wi-Fi.
4. **Student phone:** follow [docs/android-setup.md](docs/android-setup.md) to install Termux, put the data on the microSD card, connect the phone to its radio over Wi-Fi, and start:
   ```bash
   python -m meshlearn phone --data ~/storage/external-1/meshlearn --hub '!a1b2c3d4' --name "North village" --radio-host 192.168.43.20
   ```
5. **Load the assignments** while the phone is at school. Either way puts a copy of every assignment on the phone with no radio traffic:
   - **School Wi-Fi:** open `http://localhost:8080/status` in the phone's browser and enter the hub's address.
   - **Memory card:** download the course pack from the hub dashboard and unzip it into the phone's MeshLearn folder, or run:
     ```bash
     python3 -m meshlearn pack --out /Volumes/SDCARD/Android/data/com.termux/files/meshlearn
     ```
6. **Fill the library** before the phone leaves an area with internet:
   ```bash
   python3 -m meshlearn library --sd-gb 32 --lang pt --dest /Volumes/SDCARD/Android/data/com.termux/files/meshlearn/library --dry-run
   ```
   Drop `--dry-run` to download. Run it again to resume an interrupted download.

## What fits on the card

The fetcher looks up current file sizes from the Kiwix catalog every time it runs, so these plans change as the collections are updated. Example plans from October 24, 2026, keeping 2 GB free for assignments and the system:

| Card | English | Portuguese | German |
| --- | --- | --- | --- |
| 8 GB | PhET, Wikipedia 100, 10 classic books, Best of Wikipedia (mini), Wikibooks (text) — 4.3 GB | — | — |
| 16 GB | — | PhET, Wikilivros, all of Wikipedia (text only) — 7.3 GB | — |
| 32 GB | PhET, 10 classic books, Wikibooks, Wikiversity, all of Wikipedia (article intros) — 23.1 GB | — | — |
| 64 GB | — | — | PhET, Wikibooks, all of Wikipedia with images — 52.3 GB |

Run `python3 -m meshlearn library --sd-gb <size> --lang <en|pt|de> --dest <folder> --dry-run` to see the plan for any card. Content keeps its original license (Wikipedia projects: CC BY-SA; PhET: CC BY 4.0; Project Gutenberg texts: public domain in the USA).

## How the mesh link works

### What travels by radio

Assignments live on both the hub and the phone, so the radio mostly carries answers and grades. These counts are packets for a 20-question review completed by 10 students. Each message also gets a one-packet acknowledgement, not counted here.

| Step | Assignment sent by radio each time | With a stored copy and batching |
| --- | --- | --- |
| Get the assignment to students | 5 packets | **1 packet** (open message) |
| 10 students' answers | 20 packets (2 each) | **2 packets** (one batch) |
| 10 grades back | 10 packets | **1 packet** (one batch) |
| **Total** | **35 packets** | **4 packets** |

The full assignment travels by radio only when it is new or edited after the phone's copy was loaded. Even then it is 3 packets for this 20-question review, and 2 for the sample assignment.

Moving a due date or hiding an assignment is always 1 packet.

### How it stays reliable

- **Stored copies stay in step.** Every assignment carries a version stamp computed from its content. Phones report their versions when they check in. The hub sends a full copy only when a phone's copy is missing or out of date, including after an edit. If an open message reaches a phone without the right copy, the phone asks for it.
- **Answers are graded against what the student saw.** Each submission records the version it was written against, and the hub keeps every version.
- **Small packets.** Each message is compressed with zlib using a shared dictionary of words common in assignments, so short messages compress well. It is then split into 228-byte frames, under Meshtastic's 233-byte limit. Assignments use a compact format with short field names, and defaults are left out.
- **Only lost pieces are resent.** When a burst ends with pieces missing, the receiver asks for exactly those. If the sender hears nothing, it sends one probe packet instead of the whole message. At 20% packet loss, a 10-packet message costs about 13 transmissions this way, compared with about 90 if the whole message were resent.
- **Nothing is lost.** Every outgoing message is written to SQLite before it is sent. Retries back off from 30 s up to 10 min on real radios. If a phone reboots or the link is down for days, the queue picks up where it left off.
- **No duplicates.** The receiver remembers which messages it has handled. It re-acknowledges a repeat without acting on it twice.
- **The answer key stays home.** In graded mode, the phone's copy has no answers, so nothing on the memory card gives them away, and grading happens on the hub. In practice mode the key is included, so students get instant right/wrong feedback.
- **Separate from chat.** MeshLearn uses Meshtastic's `PRIVATE_APP` port, so ordinary Meshtastic text messages on the same mesh are unaffected.

## Accessibility and adaptation built in

- Works in any browser on any device that can join Wi-Fi; no app install for students.
- Large touch targets, visible labels on every field, error messages linked to their fields for screen readers, and light and dark themes that follow the device.
- Numbers accept `3.5`, `3,5` (comma decimals used in Brazil and Germany), and fractions like `3/4`.
- Library language per phone (English, Portuguese, German); text-only and smaller editions chosen automatically for small cards.
- Students identify themselves by name only. No accounts, passwords, or personal data beyond what the teacher needs.

## Project layout

```
meshlearn/
  protocol.py        message format: dictionary compression, framing, missing-piece reports
  transport.py       simulated radio (UDP) and real Meshtastic radios (USB or Wi-Fi)
  messenger.py       store-and-forward delivery: acks, selective resend, de-duplication
  assignments.py     question types, compact format, version stamps, answer checking, grading
  hub.py             teacher dashboard, open/hide per phone, course pack export
  phone.py           stored assignments, student website, batching, library browser
  library.py         open-content planner and downloader
  web.py             tiny standard-library web framework
  starter_library/   three short original lessons included on every phone
  sample_assignment.json
tests/test_meshlearn.py
docs/hardware.md, docs/android-setup.md
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

There are 25 tests. They cover:

- framing, corruption, and missing-piece reports;
- lossless compact format;
- version stamps;
- grading and validation;
- the library planner;
- delivery through 40% simulated loss, with each message delivered exactly once;
- selective resend staying under 4× the message size at 30% loss.

End-to-end tests run through both websites:

- radio-only delivery, then grading and results;
- a Wi-Fi sync followed by a 1-packet open;
- a memory-card course pack;
- a phone fetching a copy it was missing;
- editing an assignment after students answered;
- three students' work sharing one radio message;
- library traversal protection.

## Status and limits

Tested:

- Everything above, on Python 3.9 on macOS, using the simulated radio.
- The library fetcher's live lookups against the Kiwix catalog and Project Gutenberg, and a real download.

Not yet tested:

- **Real Meshtastic radios.** `MeshtasticTransport` follows the Meshtastic Python API but has not been run against hardware.
- **Termux on Android.** The steps in the setup guide have not been run on a phone yet.
- **Airtime and range at the pilot site.** Field testing will set the retry timing and the submission batch window, which defaults to 120 s. It will also show how many relays a site needs.

Still to build:

- Multimedia in assignments.
- Teacher accounts.
- Over-the-air library updates.
- Encryption beyond Meshtastic's channel key.

## Team

Karen Makena, Yitzchuk Rosenblat, Bruck Asefa, James Shamshoian, Fiona Acquah, with partners at H-BRS (Germany) and Instituto Mauá (Brazil).
