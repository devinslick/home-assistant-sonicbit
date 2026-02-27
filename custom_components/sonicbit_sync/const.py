"""Constants for the SonicBit Media Sync integration."""

DOMAIN = "sonicbit_sync"

# Config entry keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_STORAGE_PATH = "storage_path"

# Defaults
DEFAULT_STORAGE_PATH = "/media/sonicbit"
POLL_INTERVAL = 60  # seconds between API polls

# Download settings
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB chunks for streaming downloads

# Sensor status values
STATUS_IDLE = "Idle"
STATUS_DOWNLOADING = "Downloading"
STATUS_ERROR = "Error"
