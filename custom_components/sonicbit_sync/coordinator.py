"""DataUpdateCoordinator and download engine for SonicBit Media Sync."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CHUNK_SIZE,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_STORAGE_PATH,
    DEFAULT_STORAGE_PATH,
    DOMAIN,
    POLL_INTERVAL,
    STATUS_DOWNLOADING,
    STATUS_ERROR,
    STATUS_IDLE,
)
from .token_handler import HATokenHandler

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION = 1


class SonicBitCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the SonicBit API and orchestrates file transfers.

    Architecture notes
    ------------------
    * The sonicbit SDK is fully synchronous (httpx, no async client).  Every
      SDK call is therefore dispatched to HA's thread-pool executor via
      ``hass.async_add_executor_job`` so the event-loop is never blocked.
    * Long-running file downloads run as background tasks so the 60-second
      coordinator refresh cycle is never delayed.
    * A ``_downloading`` set prevents the same torrent from being queued
      twice while a transfer is already in progress.
    * ``_completed_names`` persists across restarts so successfully-downloaded
      folders are never deleted during stale-folder cleanup.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL),
        )
        self._entry = entry
        self._email: str = entry.data[CONF_EMAIL]
        self._password: str = entry.data[CONF_PASSWORD]
        self._storage_path: str = entry.data.get(CONF_STORAGE_PATH, DEFAULT_STORAGE_PATH)

        self._client = None  # Lazy-initialised in executor thread
        self._downloading: set[str] = set()

        # Public state read by sensor entities
        self.status: str = STATUS_IDLE
        self.storage_percent: float = 0.0

        # Persistent store: tracks torrent names successfully downloaded by
        # this integration so their local folders are never removed by cleanup.
        # None means "not yet loaded from disk".
        self._store = Store(
            hass,
            _STORE_VERSION,
            f"{DOMAIN}_completed_{entry.entry_id}",
        )
        self._completed_names: set[str] | None = None

    # ------------------------------------------------------------------
    # SonicBit client – created once, reused thereafter
    # ------------------------------------------------------------------

    def _get_client(self):
        """Return (and lazily create) the SonicBit SDK client.

        Must be called from an executor thread only.
        """
        if self._client is None:
            from sonicbit import SonicBit  # noqa: PLC0415

            handler = HATokenHandler(
                self.hass.config.config_dir,
                self._entry.entry_id,
            )
            self._client = SonicBit(
                email=self._email,
                password=self._password,
                token_handler=handler,
            )
        return self._client

    # ------------------------------------------------------------------
    # DataUpdateCoordinator hook – runs every POLL_INTERVAL seconds
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch storage stats and kick off a sync if nothing is running."""
        try:
            storage = await self.hass.async_add_executor_job(self._fetch_storage)
            self.storage_percent = storage.percent
        except Exception as err:
            # Log the error and surface it through the status sensor instead of
            # raising UpdateFailed (which would make entities show "unavailable"
            # rather than the more informative "Error" state).
            _LOGGER.error("SonicBit API error: %s", err)
            self.status = STATUS_ERROR
            return {
                "storage_percent": self.storage_percent,
                "status": self.status,
            }

        # Storage fetch succeeded; clear a previous API-error status when idle.
        if self.status == STATUS_ERROR and not self._downloading:
            self.status = STATUS_IDLE

        # Trigger sync only when no download is already active
        if not self._downloading:
            self.hass.async_create_task(self._trigger_sync())

        return {
            "storage_percent": self.storage_percent,
            "status": self.status,
        }

    # ------------------------------------------------------------------
    # Sync orchestration
    # ------------------------------------------------------------------

    async def _trigger_sync(self) -> None:
        """List torrents, clean up stale local folders, queue new downloads."""
        try:
            all_torrents = await self.hass.async_add_executor_job(
                self._list_all_torrents
            )
        except Exception as err:
            _LOGGER.error("Failed to list torrents: %s", err)
            return

        completed = [t for t in all_torrents if t.progress == 100]
        active_names = {t.name for t in all_torrents}

        # Initialise persistent store on the first call
        if self._completed_names is None:
            await self._load_completed_names()

        # Remove local folders whose torrents no longer exist on SonicBit
        await self._cleanup_stale_folders(active_names)

        for torrent in completed:
            if torrent.hash not in self._downloading:
                _LOGGER.info(
                    "Queuing download for completed torrent: %s (hash=%s)",
                    torrent.name,
                    torrent.hash,
                )
                self._downloading.add(torrent.hash)
                self.hass.async_create_task(self._process_torrent(torrent))

    async def _load_completed_names(self) -> None:
        """Load completed-download names from the persistent store.

        On the very first run (no store file yet) every existing local folder
        is treated as "completed" so that pre-existing downloads are not
        accidentally deleted by the new cleanup logic.
        """
        data = await self._store.async_load()
        if data is not None:
            self._completed_names = set(data.get("names", []))
            _LOGGER.debug(
                "Loaded %d completed torrent name(s) from store",
                len(self._completed_names),
            )
        else:
            # First run: seed from whatever is already on disk
            existing = await self.hass.async_add_executor_job(
                self._scan_local_folders
            )
            self._completed_names = existing
            await self._store.async_save({"names": list(self._completed_names)})
            _LOGGER.debug(
                "Initialised completed-downloads store with %d existing folder(s)",
                len(self._completed_names),
            )

    async def _cleanup_stale_folders(self, active_names: set[str]) -> None:
        """Delete local folders for torrents no longer present on SonicBit.

        A folder is considered stale when:
        * its name does not match any torrent currently on SonicBit, AND
        * it was not successfully downloaded by this integration (i.e. it is
          not in ``_completed_names``).

        This handles torrents that were deleted from SonicBit before the
        integration finished downloading them, leaving behind partial folders.
        Successfully completed downloads are always preserved.
        """
        storage_path = Path(self._storage_path)
        path_exists = await self.hass.async_add_executor_job(storage_path.exists)
        if not path_exists:
            return

        try:
            subdirs = await self.hass.async_add_executor_job(
                lambda: [p for p in storage_path.iterdir() if p.is_dir()]
            )
        except Exception as err:
            _LOGGER.warning("Failed to scan storage directory: %s", err)
            return

        for folder in subdirs:
            if folder.name in active_names:
                continue  # Torrent still exists on SonicBit
            if self._completed_names is not None and folder.name in self._completed_names:
                continue  # Was fully downloaded by this integration

            _LOGGER.info(
                "Removing stale local folder '%s' (torrent no longer on SonicBit)",
                folder.name,
            )
            try:
                await self.hass.async_add_executor_job(shutil.rmtree, str(folder))
            except Exception as err:
                _LOGGER.warning(
                    "Failed to remove stale folder '%s': %s", folder.name, err
                )

    async def _process_torrent(self, torrent) -> None:
        """Download all files for a torrent then delete the cloud copy.

        Per-torrent error handling ensures a failed file prevents deletion
        of the cloud copy (so no data is lost) while still allowing other
        torrents to be processed independently.
        """
        torrent_name: str = torrent.name
        torrent_hash: str = torrent.hash

        self.status = STATUS_DOWNLOADING
        self._notify_listeners()
        _LOGGER.info("Starting transfer for torrent: %s", torrent_name)

        all_ok = True
        try:
            details = await self.hass.async_add_executor_job(
                self._get_torrent_details, torrent_hash
            )
            if not details.files:
                _LOGGER.warning("Torrent %s has no files; skipping", torrent_name)
                return

            for torrent_file in details.files:
                try:
                    await self._download_file(torrent_name, torrent_file)
                except Exception as err:
                    _LOGGER.error(
                        "Failed to download '%s' from torrent '%s': %s",
                        torrent_file.name,
                        torrent_name,
                        err,
                    )
                    all_ok = False

            if all_ok:
                await self.hass.async_add_executor_job(
                    self._delete_torrent, torrent_hash
                )
                _LOGGER.info(
                    "Transfer complete – deleted cloud copy of '%s'", torrent_name
                )
                # Record completion so the local folder is preserved by cleanup
                if self._completed_names is not None:
                    self._completed_names.add(torrent_name)
                    await self._store.async_save(
                        {"names": list(self._completed_names)}
                    )
            else:
                _LOGGER.warning(
                    "One or more files failed for '%s'; cloud copy retained",
                    torrent_name,
                )
        except Exception as err:
            _LOGGER.error("Unexpected error processing torrent '%s': %s", torrent_name, err)
            all_ok = False
        finally:
            self._downloading.discard(torrent_hash)
            self.status = STATUS_ERROR if not all_ok else (
                STATUS_IDLE if not self._downloading else STATUS_DOWNLOADING
            )
            self._notify_listeners()

    # ------------------------------------------------------------------
    # File download engine
    # ------------------------------------------------------------------

    async def _download_file(self, torrent_name: str, torrent_file) -> None:
        """Stream a single cloud file to local storage with atomic rename.

        Files are written to ``<storage_path>/<torrent_name>/<filename>.tmp``
        and renamed to their final name only after the size is verified.
        This prevents DLNA / media scanners from indexing partial files.
        """
        dest_dir = Path(self._storage_path) / torrent_name
        await self.hass.async_add_executor_job(
            lambda: dest_dir.mkdir(parents=True, exist_ok=True)
        )

        final_path = dest_dir / torrent_file.name
        tmp_path = dest_dir / (torrent_file.name + ".tmp")

        # Skip if already fully present (e.g. after a previous partial run)
        if final_path.exists() and final_path.stat().st_size == torrent_file.size:
            _LOGGER.debug("Already downloaded, skipping: %s", torrent_file.name)
            return

        _LOGGER.info(
            "Downloading '%s' (%s bytes) → %s",
            torrent_file.name,
            f"{torrent_file.size:,}",
            dest_dir,
        )

        await self.hass.async_add_executor_job(
            self._stream_download,
            torrent_file.download_url,
            tmp_path,
            final_path,
            torrent_file.size,
        )

    @staticmethod
    def _stream_download(
        url: str,
        tmp_path: Path,
        final_path: Path,
        expected_size: int,
    ) -> None:
        """Blocking chunked download with post-transfer size verification.

        Uses httpx (already a sonicbit dependency) with streaming enabled so
        that multi-gigabyte files are written chunk-by-chunk and never held
        in RAM all at once.

        Raises an exception on size mismatch; the .tmp file is removed
        before raising so no partial artefacts are left behind.
        """
        try:
            with httpx.stream("GET", url, timeout=httpx.Timeout(10.0, read=None)) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=CHUNK_SIZE):
                        fh.write(chunk)

            actual_size = os.path.getsize(tmp_path)
            if actual_size != expected_size:
                raise ValueError(
                    f"Size mismatch for '{tmp_path.name}': "
                    f"downloaded {actual_size:,} bytes, expected {expected_size:,}"
                )

            # Atomic rename – the file is only visible to scanners once complete
            tmp_path.rename(final_path)
            _LOGGER.info("Saved: %s", final_path)

        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------
    # Thin executor wrappers around synchronous SDK calls
    # ------------------------------------------------------------------

    def _fetch_storage(self):
        return self._get_client().get_storage_details()

    def _list_all_torrents(self) -> list:
        result = self._get_client().list_torrents()
        torrents = list(result.torrents.values())
        _LOGGER.debug(
            "Polled SonicBit: %d torrent(s) total, %d completed",
            len(torrents),
            len([t for t in torrents if t.progress == 100]),
        )
        return torrents

    def _scan_local_folders(self) -> set[str]:
        """Return names of all subfolders in the storage path (blocking)."""
        storage_path = Path(self._storage_path)
        if not storage_path.exists():
            return set()
        try:
            return {p.name for p in storage_path.iterdir() if p.is_dir()}
        except Exception:
            return set()

    def _get_torrent_details(self, torrent_hash: str):
        return self._get_client().get_torrent_details(torrent_hash)

    def _delete_torrent(self, torrent_hash: str) -> None:
        self._get_client().delete_torrent(torrent_hash, with_file=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _notify_listeners(self) -> None:
        """Push the current status to all registered HA listeners."""
        self.async_set_updated_data(
            {
                "storage_percent": self.storage_percent,
                "status": self.status,
            }
        )

    async def async_force_sync(self) -> None:
        """Public method called by the Force Sync button."""
        _LOGGER.info("Manual sync triggered via button")
        if not self._downloading:
            await self._trigger_sync()
        else:
            _LOGGER.info("Sync already in progress; ignoring manual trigger")
