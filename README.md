# Alarm Clock & Bed for Home Assistant

Custom integration for **bedside tablets**: any number of alarms per bed side, played on the tablet (room panel card)
and/or a `media_player`, plus per bed side **weight / occupancy** from load cells and a **heated blanket controller** with
over-temperature protection. A watchdog wakes you even if the tablet's dashboard hangs (via Fully Kiosk).
All alarms, settings and the event log are stored in **MariaDB/MySQL** (local fallback copy in `.storage`).

- Domain: `wecker` (German name kept for compatibility; config keys, services and entity ids are German too)
- Version: 1.4.0
- UI language: German. Bed sides default to `links` (left) and `rechts` (right).

> Built for the author's own bedroom setup (two bed tablets, load cells, 12 V heated underblankets). It works
> generically, but expect to adapt entities in the settings.

## Features

### Alarms
- Any number of alarms per bed side: time, name, weekdays (empty = one-time), sound, snooze (duration, max. count),
  rising volume (start/end volume, ramp duration), lock for the off button (**math task** / **hold 3 s** / none),
  auto-stop after n minutes, "skip next".
- Random sound per ring, vibration alarm (pattern, strength, delay) via a switch/number/select (e.g. an ESP32 bed vibrator).
- Sound system output while ringing: volume/EQ number entities, power switch, source select – restored afterwards.
- Catches up a missed alarm (HA was down) for up to 15 minutes.
- Alarm sounds: files in `/config/www/wecker/toene` (wav, mp3, ogg, m4a, flac). A set of generated sounds and the
  generator script are included in [`sounds/`](sounds/) (`node sounds/_erzeugen.js <target folder>`).
- **Tablet watchdog**: the panel reports regularly; if a Fully Kiosk tablet is configured for a bed side and the
  panel does not report a playing sound 20 s after the alarm starts, the integration turns the screen on, reloads the
  dashboard and plays the alarm natively through the Fully Kiosk media player with rising volume (emergency wake-up).
  It also reloads a silent tablet shortly before an alarm.

### Bed
- **Weight** per side: one total sensor or four load cells (head/foot, inner/outer), minus a tare weight ("tare now").
- **Occupancy**: `frei` (free), `belegt` (occupied); with four cells also `liegt` (lying) / `sitzt` (sitting).
- **Heated blanket** per side: switch/light/fan/input_boolean (two-point control with hysteresis on the mattress
  temperature) or `climate`.
- Safety (runs in HA): runtime always limited, immediate off and lock-out above the protection temperature until
  5 °C cooler, stays off without a temperature value, turned off when HA stops.
- Bed tablet background settings (aurora, sky with real sun/moon position, color, image slideshow) stored per side.

## Entities

Per bed side `<seite>` (default `links`, `rechts`):

| Entity | Description |
|---|---|
| `sensor.wecker_<seite>` | `bereit` / `klingelt` / `schlummert` / `aus` (ready / ringing / snoozing / off), next alarm in attributes |
| `sensor.wecker_<seite>_naechster` | Timestamp of the next alarm (e.g. for a wake-up light 20 min before) |
| `sensor.bett_<seite>_gewicht` | Bed weight (kg) |
| `sensor.bett_<seite>_belegung` | Occupancy |
| `sensor.bett_<seite>_heizdecke` | Heated blanket: `aus` / `heizt` / `haelt` / `schutz` / `fehler` |

Events: `wecker_klingelt` (ringing), `wecker_schlummert` (snoozing), `wecker_aus` (stopped) with `alarm_id`, `name`,
`seite`, `time`, `sound`; `wecker_heizdecke` (heated blanket on/off/protection).

## Services

| Service | Description |
|---|---|
| `wecker.snooze` | Snooze ringing alarms (`seite` or `alarm_id`) |
| `wecker.stop` | Stop ringing/snoozing alarms (`seite` or `alarm_id`) |
| `wecker.test` | Ring an alarm now (`alarm_id`) |
| `wecker.reload_sounds` | Rescan the sound folder |
| `wecker.heizdecke_an` | Heated blanket on (`seite`, `temperatur`, `dauer_min`, `dauerhaft`) |
| `wecker.heizdecke_aus` | Heated blanket off (`seite`) |

WebSocket API used by the card: `wecker/subscribe`, `wecker/alarm/save|delete`, `wecker/snooze`, `wecker/stop`,
`wecker/test`, `wecker/settings/save`, `wecker/events`, `wecker/sounds`, `wecker/bett/heat`, `wecker/bett/tara`,
`wecker/vib_test`, `wecker/alive`, `wecker/tablets`.

## Requirements

- Home Assistant 2024.6 or newer
- MariaDB or MySQL (the Home Assistant container already ships the `mysqlclient` driver for `mysql://` URLs);
  SQLite also works for testing.
- Optional: Fully Kiosk Browser integration (watchdog), media players, load cell / temperature sensors, heated blanket switch

## Installation

### HACS (custom repository)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/SysriX/alarm_clock_ha_integration`, category **Integration**.
2. Install **Alarm Clock & Bed** and restart Home Assistant.
3. Copy the sounds from [`sounds/`](sounds/) (or your own files) to `/config/www/wecker/toene/`.

### Manual

Copy `custom_components/wecker` to `/config/custom_components/wecker` and restart Home Assistant.

## Configuration

1. Create the database once with [`setup-db.sql`](setup-db.sql) (replace `CHANGE_ME`). Tables are created automatically.
2. `secrets.yaml`:
   ```yaml
   wecker_db_url: mysql://wecker:YOUR_PASSWORD@db.example.lan/wecker?charset=utf8mb4
   ```
3. `configuration.yaml`:
   ```yaml
   wecker:
     db_url: !secret wecker_db_url
     seiten: [links, rechts]   # optional bed sides (slugs), default: links + rechts
   ```
4. Restart. Alarms and all bed/output settings are edited in the room panel's alarm and bed settings modules
   (admin account required for settings).

Tablet tips (Fully Kiosk Browser): enable *Autoplay Videos/Audio* (otherwise Android blocks the alarm sound) and the
*JavaScript Interface* (screen on and system volume ramp while ringing).

## Companion card

The alarm clock, clock, bed and bed-settings modules are part of the room panel card
[room_panel_ha_card](https://github.com/SysriX/room_panel_ha_card) (`custom:raum-panel`, modules `uhr`, `wecker`, `bett`, …).

## License

MIT – see [LICENSE](LICENSE).
