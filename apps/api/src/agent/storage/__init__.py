"""Artifact I/O. No module outside this package may touch a filesystem path."""

from agent.storage.backend import StorageBackend, StorageError, get_storage

__all__ = ["StorageBackend", "StorageError", "get_storage"]
