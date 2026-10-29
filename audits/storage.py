"""Read-only Django storage over ``StoredFile`` rows (review instance).

The review instance has no capture files on disk; publish copied image bytes
into the shared DB under the same storage name the laptop used, so existing
``shot.image.url`` / ``.open()`` code works unchanged.
"""

from __future__ import annotations

from urllib.parse import quote

from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.urls import reverse
from django.utils.deconstruct import deconstructible


@deconstructible
class DatabaseStorage(Storage):
    def _rows(self):
        from audits.models import StoredFile

        return StoredFile.objects

    def _open(self, name, mode="rb"):
        if "w" in mode or "a" in mode:
            raise OSError("DatabaseStorage is read-only")
        row = self._rows().filter(name=name).only("data").first()
        if row is None:
            raise FileNotFoundError(name)
        return ContentFile(bytes(row.data), name=name)

    def _save(self, name, content):
        raise OSError("DatabaseStorage is read-only; images arrive via publish")

    def exists(self, name):
        return self._rows().filter(name=name).exists()

    def size(self, name):
        size = self._rows().filter(name=name).values_list("size", flat=True).first()
        if size is None:
            raise FileNotFoundError(name)
        return size

    def delete(self, name):
        raise OSError("DatabaseStorage is read-only")

    def url(self, name):
        return reverse("stored_media", kwargs={"name": quote(name)})

    def thumb_url(self, name):
        return self.url(name) + "?thumb=1"
