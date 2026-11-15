# Recommended hardware

A minimal pilot needs **two radios and one old phone**. Add relay radios to reach farther households.

Prices are rough retail figures as of late 2026 and vary by seller and country; check before ordering. All boards listed run the free, open-source [Meshtastic](https://meshtastic.org) firmware.

## 1. Pick the right frequency first

Buy every radio and antenna for the same band, and the band must be legal where the radios will be used. Set the matching **region** in Meshtastic after flashing.

| Country | Band to buy | Meshtastic region | Notes |
| --- | --- | --- | --- |
| United States | 915 MHz | `US` | License-free under FCC Part 15 at Meshtastic's default power. |
| Brazil | 915 MHz | Check the Meshtastic region list for Brazil | ANATEL allows LoRa in parts of the 902–928 MHz band; confirm the allowed sub-band with the Brazilian partners. |
| Germany | 868 MHz | `EU_868` | EU duty-cycle limits apply (the radio may only transmit a small share of the time), so large assignments take longer to send. |
| Kenya | 868 MHz is typical | Confirm with the Communications Authority of Kenya | Example non-partner country for the TSR02 comparison. |

Meshtastic on these ISM bands does not need an amateur (ham) radio license. A ham license only matters if the team turns on Meshtastic's licensed mode, which raises power limits but forbids encryption.

## 2. Radios

| Role | Recommended board | Why | Rough price |
| --- | --- | --- | --- |
| **Phone's radio** (needs Wi-Fi so the phone can reach it) | **Heltec WiFi LoRa 32 V3** (ESP32-S3 + SX1262) | Cheap, Wi-Fi built in, small screen, USB-C. Joins the phone's hotspot so MeshLearn talks to it over Wi-Fi. | US$20–25 |
| Phone's radio, battery-friendly alternative | **LILYGO T-Beam** (SX1262 version) | Wi-Fi, holder for a single 18650 cell, GPS for mapping coverage during field tests. | US$35–45 |
| **Teacher hub radio** (USB to laptop) | Heltec V3 or **RAK WisBlock Meshtastic Starter Kit** (RAK4631) | Any Meshtastic board works over USB. The RAK kit uses very little power. | US$20–35 |
| **Relay** (on a roof or pole between school and homes) | **RAK WisBlock (RAK4631)** in a weatherproof box with a small solar panel and battery | The nRF52 chip sips power, so a small panel can run it indefinitely. Set its Meshtastic role to `ROUTER`. | US$60–100 with enclosure, panel, battery |

Avoid boards with the older SX1276 radio chip for new purchases; the SX1262 boards above have better range per milliwatt.

## 3. Antennas and power

- **Never power on a radio without its antenna attached.** It can damage the radio chip.
- The stock antennas work for bench tests. For the field, a tuned 3–5 dBi antenna of the right band, mounted as high as possible, matters more than anything else for range.
- Phone and radio at a home: one USB phone charger with two ports, or a 10,000 mAh power bank, runs both through the day. Where grid power is unreliable, a 10–20 W solar panel with a USB charge controller and power bank keeps them going.
- Relay: 5–6 W panel, 18650 or LiPo battery, and a charge controller. Point the panel at the equator.

## 4. The community phone

Any Android phone that meets these will do; donated or second-hand phones are ideal.

- Android 7 or newer, so current Termux runs.
- A **microSD slot**. Card size sets the library: 32 GB gets full offline Wikipedia (intros) plus textbooks; 64–128 GB gets Wikipedia with images.
- Wi-Fi hotspot that still works.
- A battery that holds a charge, or keep it plugged in.

**MicroSD cards:** buy an A1 or A2 rated card from a known brand. Cheap counterfeit cards often report a fake size and lose data once filled.

## 5. Shopping list for a two-site pilot

| Item | Qty | Rough total |
| --- | --- | --- |
| Heltec WiFi LoRa 32 V3 (right band) with antenna | 2 | US$45 |
| 3–5 dBi outdoor antenna (right band) | 2 | US$20–40 |
| 64 GB A1/A2 microSD card | 1 | US$8–12 |
| Old Android phone with microSD slot | 1 | donated |
| USB-C cables, 2-port charger, power bank | 1 set | US$25 |
| Optional solar relay (RAK4631 kit, enclosure, panel, battery) | 1 | US$60–100 |
| **Total without relay** | | **about US$100–125** |

## 6. Setting up the radios

1. Flash Meshtastic from https://flasher.meshtastic.org (Chrome or Edge, USB cable).
2. In the Meshtastic app, set the **region** from the table above.
3. **Give MeshLearn its own channel.** Channel 0 is the public default: other people's radios nearby use it, and their relays would carry MeshLearn traffic. Add a secondary channel with a random key on one radio, then copy the same key to the others. With the Meshtastic command line:
   ```bash
   meshtastic --port /dev/ttyUSB0 --ch-add MeshLearn          # first radio; note the slot it reports, e.g. 3
   meshtastic --port /dev/ttyUSB0 --info                      # copy the MeshLearn channel's psk
   meshtastic --port /dev/ttyUSB1 --ch-add MeshLearn          # each other radio
   meshtastic --port /dev/ttyUSB1 --ch-index 3 --ch-set psk base64:<the psk>
   ```
   Then start the hub and phone with `--radio-channel 3` (the slot number).
4. **Phone's radio:** under Network, turn on Wi-Fi and enter the phone hotspot's name and password. Note the IP address the radio gets (shown on the Heltec screen or in the Meshtastic app). On ESP32 boards Bluetooth switches off while Wi-Fi is on; LoRa keeps working.
5. **Relay:** set Device role to `ROUTER`.
6. Write down each radio's node id (looks like `!a1b2c3d4`). The hub needs the phone radio's id, and the phone needs the hub radio's id.

## 7. Bench test with two radios

Before going to the field, plug both radios into one computer and run:

```bash
pip install meshtastic
python3 -m meshlearn radio-test --a /dev/ttyUSB0 --b /dev/ttyUSB1 --channel 3 --hops 0
```

Radio A plays the hub and radio B the phone. The test sends the four messages MeshLearn uses most and times each one. `--hops 0` stops other radios from relaying the test packets. On macOS the ports look like `/dev/cu.usbmodem101`.

Results on 2026-11-15, with a RAK4631 (firmware 2.7.26) and a Seeed Wio Tracker L1 (firmware 2.8.0) about 1 m apart, US region, `LONG_TURBO` preset:

| Message | Packets | Arrived | Confirmed | Resends |
| --- | --- | --- | --- | --- |
| Open an assignment the phone already has | 1 | 2.6 s | 3.9 s | 0 |
| A batch of 5 students' answers | 1 | 4.1 s | 5.1 s | 0 |
| A full assignment the phone doesn't have | 2 | 6.4 s | 7.8 s | 0 |
| A batch of 5 grades | 1 | 1.7 s | 3.3 s | 0 |

The real hub and phone then ran a full cycle over the same radios. A new assignment reached the student site in 5.7 s, a student's answers reached the hub 7 s after they pressed Submit (5 s of that was the phone's batching wait), and the grade was back on the student site 7 s after the teacher saved it.

What the test found:

- **Packets over 219 bytes never arrive.** Meshtastic advertises 233 bytes, but current firmware reserves room for encryption and silently drops anything larger. MeshLearn now uses 219-byte frames; with 228-byte frames every multi-packet message failed.
- **RAK4631 and Wio Tracker L1 have no Wi-Fi.** They work well as the hub's radio (USB) or a relay, but the phone's radio needs Wi-Fi, so use a Heltec V3 or T-Beam there.

Still to measure: range outdoors, and timing with relays in between.
