"""Compatibility patches for the sonicbit SDK.

The sonicbit API occasionally returns numeric values for fields that the SDK
model declares as ``str``.  These patches coerce the offending values inside
``__init__`` – before Pydantic's compiled validator runs – so the integration
works regardless of what the live API returns.

Each patch is idempotent: a sentinel attribute prevents it from being applied
more than once per interpreter session.
"""

from __future__ import annotations

import functools
import logging

_LOGGER = logging.getLogger(__name__)


def apply_sonicbit_patches() -> None:
    """Apply all sonicbit SDK compatibility patches.

    Safe to call multiple times; each individual patch guards itself with a
    sentinel so the monkey-patching only happens once.
    """
    _patch_torrent_info_upload_rate()
    _patch_user_details_days_left()


def _patch_torrent_info_upload_rate() -> None:
    """Coerce ``TorrentInfo.upload_rate`` from int/float to str.

    The live SonicBit API returns ``upload_rate`` as an integer (e.g. ``0``)
    while the SDK model declares it as ``str``.
    """
    try:
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
        _LOGGER.debug("Applied sonicbit TorrentInfo.upload_rate compatibility patch")
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Could not patch sonicbit TorrentInfo model: %s", exc)


def _patch_user_details_days_left() -> None:
    """Coerce ``UserDetails.days_left`` from int to str.

    The live SonicBit API returns ``days_left`` as an integer (e.g. ``361``)
    while the SDK model declares it as ``Optional[str]``.
    """
    try:
        from sonicbit.models import user_details as _ud  # noqa: PLC0415

        cls = _ud.UserDetails
        if getattr(cls, "_days_left_coerce_patched", False):
            return

        original_init = cls.__init__

        @functools.wraps(original_init)
        def _patched_init(self, *args, **kwargs):
            if isinstance(kwargs.get("days_left"), (int, float)):
                kwargs["days_left"] = str(kwargs["days_left"])
            original_init(self, *args, **kwargs)

        cls.__init__ = _patched_init
        cls._days_left_coerce_patched = True  # type: ignore[attr-defined]
        _LOGGER.debug("Applied sonicbit UserDetails.days_left compatibility patch")
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Could not patch sonicbit UserDetails model: %s", exc)
