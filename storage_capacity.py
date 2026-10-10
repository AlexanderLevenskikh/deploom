"""Check the actual destination volume before allocating a known private copy."""
from pathlib import Path
import shutil

RESERVE_BYTES = 256 * 1024 * 1024


class StorageCapacityError(OSError):
    pass


def require_capacity(destination, byte_count, operation):
    destination = Path(destination).absolute()
    parent = destination
    while not parent.exists() and parent != parent.parent: parent = parent.parent
    required = max(0, int(byte_count)) + RESERVE_BYTES
    free = shutil.disk_usage(parent).free
    if free < required:
        raise StorageCapacityError(f"DISK_SPACE_LOW: {operation}: destination={destination}; freeBytes={free}; requiredBytes={required}; preserved data is unchanged")
