"""DataUpdateCoordinator and download engine for SonicBit Media Sync."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import time

import httpx

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CHUNK_SIZE,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REMOTE_FOLDER,
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

# How many consecutive poll failures must occur before STATUS_ERROR is set.
# A single transient network blip won't flip the sensor to error state.
_API_ERROR_THRESHOLD = 3

# Retry settings for streaming file downloads.
_DOWNLOAD_MAX_RETRIES = 3
# Exceptions considered transient for download retries (transport-level only).
_DOWNLOAD_RETRY_EXC = (httpx.TransportError,)


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
        self._remote_folder: str = entry.data.get(CONF_REMOTE_FOLDER, "")

        self._client = None  # Lazy-initialised in executor thread
        self._downloading: set[str] = set()
        self._consecutive_api_errors: int = 0

        # Public state read by sensor/switch entities
        self.status: str = STATUS_IDLE
        self.storage_percent: float = 0.0
        self.auto_delete: bool = True

        # Persistent store: tracks torrent names successfully downloaded by
        # this integration so their local folders are never removed by cleanup.
        # None means "not yet loaded from disk".
        self._store = Store(
            hass,
            _STORE_VERSION,
            f"{DOMAIN}_completed_{entry.entry_id}",
        )
        self._completed_names: set[str] | None = None
        # Hashes of torrents added via the add_torrent service; used to scope
        # sync to only integration-managed torrents when a remote folder is set.
        self._managed_hashes: set[str] | None = None

    # ------------------------------------------------------------------
    # SonicBit client – created once, reused thereafter
    # ------------------------------------------------------------------

    def _get_client(self):
        """Return (and lazily create) the SonicBit SDK client.

        Must be called from an executor thread only.
        """
        if self._client is None:
            from sonicbit import SonicBit  # noqa: PLC0415

            self._patch_sonicbit_models()
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

    @staticmethod
    def _patch_sonicbit_models() -> None:
        """Work around sonicbit's TorrentInfo.upload_rate being typed as str
        while the live API returns int or float values.

        TorrentList.from_response constructs TorrentInfo via __init__ keyword
        arguments, passing upload_rate directly from the raw JSON (which can be
        an int like 0).  We wrap __init__ to coerce numeric upload_rate values
        to str before Pydantic's compiled validator runs.  This is more
        reliable than trying to mutate model_config or __annotations__ and
        calling model_rebuild, because model_rebuild does not always update the
        compiled __pydantic_validator__ for already-defined fields.
        """
        try:
            import functools  # noqa: PLC0415

            from sonicbit.models.torrent import torrent_info as _ti  # noqa: PLC0415

            cls = _ti.TorrentInfo
            if getattr(cls, "_upload_rate_coerce_patched", False):
                return

            original_init = cls.__init__

            @functools.wraps(original_init)
            def _patched_init(self, *args, **kwargs):
                if isinstance(kwargs.get("upload_rate"), (int, float)):
                    kwargs["upload_rate"] = str(kwargs["upload_rate"])
                original_init(self, *args, **kwargs)

            cls.__init__ = _patched_init
            cls._upload_rate_coerce_patched = True  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Could not patch sonicbit TorrentInfo model: %s", exc)

    # ------------------------------------------------------------------
    # DataUpdateCoordinator hook – runs every POLL_INTERVAL seconds
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch storage stats and kick off a sync if nothing is running."""
        try:
            storage = await self.hass.async_add_executor_job(self._fetch_storage)
            self.storage_percent = storage.percent
            self._consecutive_api_errors = 0
        except Exception as err:
            self._consecutive_api_errors += 1
            if self._consecutive_api_errors < _API_ERROR_THRESHOLD:
                # Transient blip – warn and keep the current status so the
                # sensor doesn't flicker to Error on a single dropped packet.
                _LOGGER.warning(
                    "SonicBit API error (attempt %d/%d, will retry next poll): %s",
                    self._consecutive_api_errors,
                    _API_ERROR_THRESHOLD,
                    err,
                )
            else:
                # Sustained failure – surface as an error.
                _LOGGER.error(
                    "SonicBit API error (%d consecutive failures): %s",
                    self._consecutive_api_errors,
                    err,
                )
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

        if self._remote_folder and completed:
            # Scope to only torrents that were added via this integration.
            # We use the managed-hashes set (populated in async_add_torrent)
            # rather than calling list_files(), which requires a different
            # auth mechanism (web session cookie) that the SDK does not set.
            if self._managed_hashes is None:
                await self._load_completed_names()
            completed = [
                t for t in completed
                if t.hash.lower() in (self._managed_hashes or set())
            ]

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
        """Load completed-download names and managed hashes from the persistent store.

        On the very first run (no store file yet) every existing local folder
        is treated as "completed" so that pre-existing downloads are not
        accidentally deleted by the new cleanup logic.
        """
        data = await self._store.async_load()
        if data is not None:
            self._completed_names = set(data.get("names", []))
            self._managed_hashes = set(data.get("managed_hashes", []))
            _LOGGER.debug(
                "Loaded %d completed torrent name(s) and %d managed hash(es) from store",
                len(self._completed_names),
                len(self._managed_hashes),
            )
        else:
            # First run: seed from whatever is already on disk
            existing = await self.hass.async_add_executor_job(
                self._scan_local_folders
            )
            self._completed_names = existing
            self._managed_hashes = set()
            await self._save_store()
            _LOGGER.debug(
                "Initialised completed-downloads store with %d existing folder(s)",
                len(self._completed_names),
            )

    async def _save_store(self) -> None:
        """Persist completed names and managed hashes to the store."""
        await self._store.async_save(
            {
                "names": list(self._completed_names or set()),
                "managed_hashes": list(self._managed_hashes or set()),
            }
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
                if self.auto_delete:
                    _LOGGER.info(
                        "All files downloaded for '%s'; deleting seedbox copy (hash=%s)",
                        torrent_name,
                        torrent_hash,
                    )
                    # Step 1: remove from the BitTorrent queue. If this fails
                    # the torrent remains in the list and will be retried next poll.
                    try:
                        await self.hass.async_add_executor_job(
                            self._delete_torrent, torrent_hash
                        )
                    except Exception as del_err:
                        _LOGGER.error(
                            "Failed to remove torrent queue entry for '%s' (hash=%s): %s"
                            " – will retry next poll",
                            torrent_name,
                            torrent_hash,
                            del_err,
                        )
                        all_ok = False
                    else:
                        # Step 2: delete the file/folder from My Drive. If this
                        # fails the queue entry is already gone so we can't
                        # auto-retry; log clearly and fall through to record
                        # completion so the local folder is not cleaned up.
                        try:
                            await self.hass.async_add_executor_job(
                                self._delete_drive_entry, torrent_name
                            )
                            _LOGGER.info(
                                "Transfer complete – deleted cloud copy of '%s'",
                                torrent_name,
                            )
                        except Exception as drive_err:
                            _LOGGER.warning(
                                "Removed torrent queue entry for '%s' but failed to"
                                " delete My Drive files: %s – manual cleanup may be required",
                                torrent_name,
                                drive_err,
                            )
                else:
                    _LOGGER.info(
                        "All files downloaded for '%s'; auto-delete is off, keeping cloud copy",
                        torrent_name,
                    )

                # Record completion so the local folder is preserved by stale-folder
                # cleanup. This runs whenever downloads succeeded (we are inside the
                # outer `if all_ok` block), including the case where My Drive deletion
                # failed after a successful queue removal – the torrent won't appear in
                # the list on the next poll so we must protect the folder now.
                if self._completed_names is not None:
                    self._completed_names.add(torrent_name)
                    await self._save_store()
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

        Transport-level errors (connection drops, timeouts) are retried up to
        _DOWNLOAD_MAX_RETRIES times with exponential backoff (2 s, 4 s, 8 s).
        The .tmp file is removed before each retry so partial artefacts are
        never left behind.  Non-transient errors (HTTP 4xx, size mismatch)
        are raised immediately without retrying.
        """
        for attempt in range(1, _DOWNLOAD_MAX_RETRIES + 1):
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
                return

            except Exception as exc:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                if not isinstance(exc, _DOWNLOAD_RETRY_EXC) or attempt == _DOWNLOAD_MAX_RETRIES:
                    raise
                wait = 2 ** attempt  # 2 s, 4 s, 8 s
                _LOGGER.warning(
                    "Download of '%s' failed (attempt %d/%d, retrying in %ds): %s",
                    tmp_path.name,
                    attempt,
                    _DOWNLOAD_MAX_RETRIES,
                    wait,
                    exc,
                )
                time.sleep(wait)

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

    def _add_torrent_uri(self, uri: str) -> None:
        """Add a torrent by magnet link or .torrent URL (blocking)."""
        from sonicbit.models.path_info import PathInfo  # noqa: PLC0415

        path = (
            PathInfo.from_path_key(self._remote_folder)
            if self._remote_folder
            else PathInfo.root()
        )
        self._get_client().add_torrent(uri, path=path)
        _LOGGER.info("Added torrent URI to SonicBit: %s", uri)

    def _add_torrent_file(self, file_path: str) -> None:
        """Upload a local .torrent file to SonicBit (blocking)."""
        from sonicbit.models.path_info import PathInfo  # noqa: PLC0415

        path = (
            PathInfo.from_path_key(self._remote_folder)
            if self._remote_folder
            else PathInfo.root()
        )
        self._get_client().add_torrent_file(file_path, path=path)
        _LOGGER.info("Added torrent file to SonicBit: %s", file_path)

    def _get_torrent_details(self, torrent_hash: str):
        return self._get_client().get_torrent_details(torrent_hash)

    def _delete_torrent(self, torrent_hash: str) -> None:
        # with_file=False because My Drive files are handled by _delete_drive_folder.
        self._get_client().delete_torrent(torrent_hash, with_file=False)

    def _delete_drive_entry(self, torrent_name: str) -> None:
        """Delete the torrent's file or folder from My Drive via the file manager API.

        delete_torrent() only removes the entry from the BitTorrent queue.
        The actual content lives in a separate cloud file manager ("My Drive")
        and must be removed with an explicit list_files / delete_file call.

        Multi-file torrents land as a folder; single-file torrents land as a
        bare file – both are handled by checking is_directory.

        Note: the file-manager endpoint requires a web session cookie in
        addition to the Bearer token.  If list_files() fails the deletion
        is skipped gracefully – the queue entry was already removed and the
        My Drive copy can be cleaned up manually.
        """
        client = self._get_client()
        try:
            file_list = client.list_files()
        except Exception as err:
            _LOGGER.warning(
                "Could not list My Drive to delete '%s'; skipping drive cleanup: %s",
                torrent_name,
                err,
            )
            return

        for item in file_list.items:
            if item.name == torrent_name:
                client.delete_file(item, is_directory=item.is_directory)
                _LOGGER.debug(
                    "Deleted My Drive %s '%s'",
                    "folder" if item.is_directory else "file",
                    torrent_name,
                )
                return
        _LOGGER.debug(
            "No My Drive entry named '%s' found at root (already gone or path differs)",
            torrent_name,
        )

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

    async def async_add_torrent(self, uri: str) -> None:
        """Add a torrent to SonicBit by magnet link, .torrent URL, or local file path.

        The ``uri`` is dispatched to the appropriate SDK call:
        * Paths starting with ``/`` are treated as local ``.torrent`` files and
          uploaded via ``add_torrent_file``.
        * Everything else (``magnet:`` links, ``http(s)://`` URLs) is passed
          directly to ``add_torrent``.

        When a remote folder is configured the torrent is placed in that folder
        and its hash is recorded in ``_managed_hashes`` so the scoped sync loop
        knows to process it.  Hash discovery uses a before/after diff of
        list_torrents() which avoids the file-manager API entirely.
        """
        # Ensure the persistent store is loaded before we might write to it.
        if self._managed_hashes is None:
            await self._load_completed_names()

        # For magnet links the infohash is embedded in the URI itself.
        # Persist it BEFORE the API call so that re-submitting a magnet link
        # that SonicBit already knows (duplicate rejection) still results in
        # correct hash tracking even if the add call raises an exception.
        extracted_hash: str | None = None
        if self._remote_folder and uri.startswith("magnet:"):
            import re  # noqa: PLC0415
            m = re.search(r"xt=urn:btih:([0-9a-fA-F]{40})", uri, re.IGNORECASE)
            if m:
                extracted_hash = m.group(1).lower()
                self._managed_hashes.add(extracted_hash)
                await self._save_store()
                _LOGGER.debug("Tracked managed hash from magnet URI: %s", extracted_hash)

        # Snapshot existing hashes for the diff fallback (non-magnet URIs only).
        before_hashes: set[str] = set()
        if self._remote_folder and extracted_hash is None:
            before = await self.hass.async_add_executor_job(self._list_all_torrents)
            before_hashes = {t.hash.lower() for t in before}

        if uri.startswith("/"):
            await self.hass.async_add_executor_job(self._add_torrent_file, uri)
        else:
            await self.hass.async_add_executor_job(self._add_torrent_uri, uri)

        if self._remote_folder and extracted_hash is None:
            # Non-magnet fallback: diff list_torrents() before/after the add.
            after = await self.hass.async_add_executor_job(self._list_all_torrents)
            new_hashes = {t.hash.lower() for t in after} - before_hashes
            if new_hashes:
                self._managed_hashes.update(new_hashes)
                await self._save_store()
                _LOGGER.debug(
                    "Tracked %d new managed hash(es) for folder-scoped sync: %s",
                    len(new_hashes),
                    new_hashes,
                )
            else:
                _LOGGER.warning(
                    "Could not detect hash of newly added torrent '%s'; "
                    "it may not be picked up by the scoped sync automatically",
                    uri,
                )
