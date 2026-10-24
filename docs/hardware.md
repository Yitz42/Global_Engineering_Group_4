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
2. In the Meshtastic app, set the **region** from the table above. Put all radios on the **same channel name and key**.
3. **Phone's radio:** under Network, turn on Wi-Fi and enter the phone hotspot's name and password. Note the IP address the radio gets (shown on the Heltec screen or in the Meshtastic app). On ESP32 boards Bluetooth switches off while Wi-Fi is on; LoRa keeps working.
4. **Relay:** set Device role to `ROUTER`.
5. Write down each radio's node id (looks like `!a1b2c3d4`). The hub needs the phone radio's id, and the phone needs the hub radio's id.
