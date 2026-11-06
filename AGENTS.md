# Notes for agents

This is the owner's working home sensor setup. Use this file to reinstall it, move
it to another machine, or add devices. [README.md](README.md) covers normal use.

Facts below were recorded at the first install on 2026-11-05. Re-check them on the
real hardware before relying on them.

## The owner's devices

### Zigbee coordinator (USB stick)

| Property | Value |
|---|---|
| Sold as | "Universal Wireless Gateway Stick, Dongle USB ZigBee 3.0, Dongle Plus-E" (AliExpress) |
| USB ID | `1a86:7523`, a CH340 serial bridge (kernel driver `ch341`) |
| Stable path | `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0` |
| Radio protocol | Silicon Labs EZSP, confirmed by probing. Exact chip not verified. |
| Firmware | EmberZNet 7.4.3, EZSP protocol 13 |
| Serial settings | 115200 baud, no hardware flow control |
| Zigbee2MQTT adapter | `ember` |
| IEEE address | `0x881a14fffefedc17` |

The CH340 has no serial number, so a second CH340 device would share the same
by-id path. The USB ID alone doesn't identify the radio, because many unrelated
boards use this bridge. To confirm the radio, send an ASH reset, `1A C0 38 BC 7E`,
at 115200 baud. A Silicon Labs stick answers with `1A C1 02 0B 0A 52 7E` or similar.

### Temperature and humidity sensor

| Property | Value |
|---|---|
| Sold as | "Tuya Zigbee Temperature Humidity Sensor, LCD, Battery or USB Powered" ([AliExpress 1005005872944768](https://pt.aliexpress.com/item/1005005872944768.html)) |
| Zigbee2MQTT model | `TS0201` (Tuya), natively supported |
| Manufacturer ID | `_TZ3210_qkj7rujp` |
| IEEE address | `0xa4c1382270cd9937` |
| MQTT topic | `zigbee2mqtt/0xa4c1382270cd9937` |
| Device type | Sleepy end device, reports `Battery` as its power source |
| Location | Living room |
| Reports | Temperature, humidity, battery, signal quality |
| Reset and pairing | Pinhole. Hold about 5 seconds until the antenna icon blinks. |

This sensor **can't be polled**: an MQTT `/get` fails with `No converter available`.
It exposes no writable settings, so its report interval can't be changed. The page
refreshes every 5 seconds, but readings arrive only when the sensor sends them.

The IEEE address survives re-pairing, so the MQTT topic stays the same.

### Other environment details

- First host: `predator`, Ubuntu 24.04.4, Docker 29.2.1, Compose v5.1.0.
- The Wi-Fi was on 2.4 GHz channel 7, so Zigbee channel 11 was chosen to stay clear
  of it. Changing the Zigbee channel means re-pairing every device, so check for
  Wi-Fi conflicts on a new site before first start.

## Fresh install

Follow the README's Install section, then check these on the new machine:

1. **Stick:** `lsusb` and `ls -l /dev/serial/by-id/`. Update `ZIGBEE_SERIAL_PORT`.
2. **Container user:** the compose file runs containers as UID and GID 1000. If
   `id` differs, edit each `user:` line.
3. **Serial group:** `stat -c '%g' /dev/ttyUSB0` should print `20`, which is what
   `dialout` means inside the Zigbee2MQTT image. If not, use the numeric GID in
   `group_add`. The host user doesn't need to be in `dialout`.
4. **Ports:** `ss -ltn` must not show 1883, 8080 or 8090 in use.
5. **Existing network:** see the first problem below before the first start.

Set `SENSOR_TOPIC=zigbee2mqtt/0xa4c1382270cd9937` and `SENSOR_NAME=Living room` in
`.env` for this sensor. To verify the install:

```bash
docker compose ps
docker exec mosquitto mosquitto_sub -C 1 -W 5 -t zigbee2mqtt/bridge/state
curl -s "http://$(hostname -I | cut -d' ' -f1):8090/api/readings?hours=24" | head -c 300
ss -ltn | grep -E ':(1883|8080|8090)\b'   # 1883 and 8080 must show 127.0.0.1
```

## Moving to another machine

The repository holds no secrets or data. Copy these from the old machine, or from
a backup, to keep the Zigbee network and the history:

```bash
# On the old machine: stop everything so the files are consistent
cd ~/zigbee && docker compose down

# Copy the private data over SSH. Change the destination path to match.
rsync -aR z2m-data dashboard/data .env user@new-machine:zigbee-climate-monitor/
```

Then move the USB stick, check the list in Fresh install, and run
`docker compose up -d --build`. With the same stick and the same `z2m-data/`, nothing
needs pairing again.

- **No copy of `z2m-data/`:** the old network key is gone. Zigbee2MQTT forms a new
  network, and each device must be paired again. The sensor takes about a minute.
- **No copy of `dashboard/data/`:** the reading history is gone. Recording starts
  fresh.
- **A different stick:** `z2m-data/coordinator_backup.json` may allow restoring the
  network. This hasn't been tested here, so read the
  [Zigbee2MQTT migration docs](https://www.zigbee2mqtt.io/guide/faq/) first and tell
  the owner the risks.

## Pairing

Run `scripts/pair.sh`, then ask the owner to do the physical step: **one** long press
of the sensor's pinhole, and then not to touch it. The owner has to act in another
room, and pairing can complete while a message is on its way, so check the current
state before telling them to press again.

To check whether a device is paired:

```bash
docker exec mosquitto mosquitto_sub -C 1 -W 5 -t zigbee2mqtt/bridge/devices | python3 -m json.tool | grep -E 'ieee_address|model_id'
```

## Known problems

**First start erases any network on the stick.** With `GENERATE` in the config,
Zigbee2MQTT logs `Adapter network does not match config. Leaving network...` and
replaces what's there. This happened at the first install, when a network was already
running on the stick and no backup existed. Before a first start, ask the owner
whether the stick has paired devices. If it may, don't start until they answer.

**A paired sensor keeps dropping off.** The sensor left three times during the first
pairing, each time announcing it itself. During that time the owner pressed the
pinhole many times and also found a bad USB cable on the sensor. Which of these caused
each drop wasn't proven. It stayed connected once the owner stopped pressing it and
ran it on batteries. Rule out both first, and ask the owner about presses, since only
they can see them.

**The join window closes too soon.** One opening lasts at most 254 seconds, and a
sensor that leaves after it closes can't rejoin. `scripts/pair.sh` renews the window
and keeps it open while watching.

**Sticks disappear or won't open.** Ubuntu's `brltty` can claim CH340 devices, though
its current rule for `1a86:7523` only matches behind a hub with ID `1a40:0101`. Check
`journalctl -k | grep -iE 'brltty|ttyUSB'`. Stopping it requires `sudo`, so the owner
would have to run that step.

**Use the IP, not `<hostname>.local`.** On the first host, `predator.local` resolved
to a Docker bridge address (172.21.0.1), which other devices can't reach.

**Log greps can miss errors.** Zigbee2MQTT logs contain colour codes, so `\berror\b`
doesn't match. Strip them first with `sed -E 's/\x1b\[[0-9;]*m//g'`.

**Log level persists.** A log level set over MQTT is written to `configuration.yaml`.
Set it back to `info` after debugging.

**Mosquitto's image tags.** The 2.1 series is tagged with a suffix, like
`2.1.2-alpine`. There's no plain `2.1.2`.

## Rules for changes

- Never commit `.env`, `z2m-data/`, `mosquitto/data/` or `dashboard/data/`. The
  network key is in `z2m-data/`, in both `configuration.yaml` and
  `coordinator_backup.json`.
- Keep the broker and Zigbee2MQTT UI on `127.0.0.1` unless the owner agrees to change
  it, and add authentication first. Docker-published ports bypass UFW.
- Keep the dashboard read-only and published on IPv4 only, so a public IPv6 address
  can't expose it past the router.
- Stop Zigbee2MQTT before editing `configuration.yaml`, or it may overwrite the edit.
- Image versions are pinned on purpose. Update them deliberately, and read the
  Zigbee2MQTT release notes first.
- The sudo password is the owner's, and the first host has no passwordless sudo. Give
  the owner any commands that need it.
