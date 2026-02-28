# SonicBit Media Sync

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/devinslick/home-assistant-sonicbit.svg)](https://github.com/devinslick/home-assistant-sonicbit/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A [Home Assistant](https://www.home-assistant.io/) custom integration that automatically syncs completed torrents from your [SonicBit](https://sonicb.it/) seedbox to local storage, then removes the cloud copy once the transfer is verified.

## Features

- **Automatic polling** — checks SonicBit every 60 seconds for completed torrents
- **Streaming downloads** — files are streamed in 8 MB chunks so multi-gigabyte transfers never spike HA's RAM
- **Atomic writes** — files land as `.tmp` and are renamed to their final name only after a size check passes, so DLNA/media scanners never index a partial file
- **Safe deletion** — the cloud copy is only deleted after every file in a torrent transfers successfully; failed downloads are retried on the next poll
- **Auto-delete toggle** — a switch entity lets you enable or disable seedbox cleanup without reconfiguring the integration
- **Four entities** — a storage sensor, a status sensor, a force-sync button, and an auto-delete switch

## Entities

| Entity | Type | Default | Description |
|---|---|---|---|
| `sensor.sonicbit_storage` | Sensor | — | Cloud storage used (%) |
| `sensor.sonicbit_status` | Sensor | — | `Idle`, `Downloading`, or `Error` |
| `button.sonicbit_force_sync` | Button | — | Trigger an immediate sync |
| `switch.sonicbit_auto_delete` | Switch | On | Delete the seedbox copy after a successful download |

### Auto-Delete Switch

When **on** (default), the integration deletes each torrent and its cloud files from SonicBit immediately after all local files are verified. When **off**, files are still downloaded to local storage but the seedbox copy is left untouched.

Turning the switch back **on** will cause any already-downloaded-but-not-yet-deleted torrents to be cleaned up on the next poll (within 60 seconds) — no manual action required.

The switch state is persisted across Home Assistant restarts.

## Requirements

- Home Assistant 2024.1 or newer
- A [SonicBit](https://sonicb.it/) account
- The `/media` folder (or your chosen path) must be writable by the Home Assistant process

## Installation

### Via HACS (recommended)

1. In Home Assistant, open **HACS → Integrations**.
2. Click the three-dot menu in the top-right corner and select **Custom repositories**.
3. Add `https://github.com/devinslick/home-assistant-sonicbit` with category **Integration**.
4. Search for **SonicBit Media Sync** and click **Download**.
5. Restart Home Assistant.

### Manual

1. Download the [latest release](https://github.com/devinslick/home-assistant-sonicbit/releases/latest).
2. Copy the `custom_components/sonicbit_sync` folder into your HA `config/custom_components/` directory.
3. Restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **SonicBit Media Sync**.
3. Enter your SonicBit **email**, **password**, and the **local storage path** where downloaded files should be saved (default: `/media/sonicbit`).

HA will verify your credentials before saving the entry. Once set up, the integration begins polling immediately.

## How It Works

```
Poll (every 60 s)
  └─ list_torrents()
       └─ filter progress == 100
            └─ for each completed torrent:
                 get_torrent_details(hash)   ← fetch per-file download URLs
                 for each file:
                   stream to <path>/<torrent>/<file>.tmp
                   verify size
                   rename .tmp → final name
                 if ALL files OK:
                   delete_torrent(hash, with_file=True)
```

## Storage Path Notes

On **Home Assistant OS** the `/media` folder is the standard location for user media and is accessible to the DLNA/media server. The default path `/media/sonicbit` maps to `/media/sonicbit` on the host.

If you run HA in **Docker** or **supervised**, ensure the path you configure is bind-mounted into the container.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Status sensor stuck on `Error` | Check **Settings → System → Logs** for `sonicbit_sync` entries |
| Files not appearing | Confirm the storage path exists and is writable: `ls -la /media/sonicbit` |
| Credentials rejected | Re-authenticate via the integration's **Configure** button in Devices & Services |
| Partial `.tmp` files | A previous download was interrupted; they will be cleaned up on the next successful run |
| Cloud copy not deleted | Check that `switch.sonicbit_auto_delete` is **on**; also check logs for deletion errors |

## Contributing

Pull requests and issues are welcome at [github.com/devinslick/home-assistant-sonicbit](https://github.com/devinslick/home-assistant-sonicbit/issues).

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

Uses the unofficial [sonicbit](https://github.com/devinslick/sonicbit-python-sdk) Python SDK by [devinslick](https://github.com/devinslick/sonicbit-python-sdk).
