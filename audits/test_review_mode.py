import hashlib

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from audits.models import StoredFile
from audits.storage import DatabaseStorage
from cameras.models import UserProfile
from cameras.roles import role_at_least

PNG = b"\x89PNG\r\n\x1a\nfake-image-bytes"


def stored(name="audits/9/cbw.png", data=PNG, thumb=b"jpeg-thumb"):
    return StoredFile.objects.create(
        name=name,
        content_type="image/png",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
        thumb=thumb,
    )


class ReviewRoleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("boss")
        UserProfile.objects.create(user=self.user, ims_role="admin")
        self.client.force_login(self.user)

    def test_capture_roles_capped_only_in_review(self):
        self.assertTrue(role_at_least(self.user, "technician"))
        with override_settings(DST_ROLE="review"):
            self.assertFalse(role_at_least(self.user, "technician"))
            self.assertFalse(role_at_least(self.user, "admin"))
            self.assertTrue(role_at_least(self.user, "readonly"))

    @override_settings(DST_ROLE="review")
    def test_review_blocks_writes_and_local_disk_pages(self):
        for method, path in (
            ("post", "/audits/dst/start/"),
            ("post", "/audits/dst/publish/"),
            ("post", "/audits/turn-relays/"),
            ("get", "/explorer/"),
            ("get", "/sync/"),
            ("get", "/admin/"),
        ):
            with self.subTest(path=path):
                self.assertEqual(getattr(self.client, method)(path).status_code, 403)

    @override_settings(DST_ROLE="review")
    def test_review_allows_reads_and_sign_out(self):
        self.assertEqual(self.client.get("/audits/").status_code, 200)
        self.assertNotEqual(self.client.post("/accounts/logout/").status_code, 403)


class StoredMediaTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("viewer")
        self.row = stored()
        self.url = DatabaseStorage().url(self.row.name)

    def test_requires_sign_in(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_serves_bytes_thumb_and_etag(self):
        self.client.force_login(self.user)
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "image/png")
        self.assertEqual(res.content, PNG)
        thumb = self.client.get(self.url + "?thumb=1")
        self.assertEqual((thumb["Content-Type"], thumb.content), ("image/jpeg", b"jpeg-thumb"))
        self.assertNotEqual(thumb["ETag"], res["ETag"])
        self.assertEqual(self.client.get(self.url, HTTP_IF_NONE_MATCH=res["ETag"]).status_code, 304)

    def test_missing_thumb_falls_back_to_original(self):
        self.client.force_login(self.user)
        row = stored(name="audits/9/vnc_l1.png", thumb=None)
        res = self.client.get(DatabaseStorage().url(row.name) + "?thumb=1")
        self.assertEqual((res["Content-Type"], res.content), ("image/png", PNG))

    def test_unknown_name_404(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/media-db/audits/0/nope.png").status_code, 404)


class DatabaseStorageTests(TestCase):
    def test_read_only_storage_over_stored_files(self):
        row = stored()
        storage = DatabaseStorage()
        self.assertTrue(storage.exists(row.name))
        self.assertEqual(storage.size(row.name), len(PNG))
        with storage.open(row.name) as fh:
            self.assertEqual(fh.read(), PNG)
        self.assertEqual(storage.url(row.name), "/media-db/audits/9/cbw.png")
        with self.assertRaises(OSError):
            storage.save("audits/9/new.png", __import__("io").BytesIO(b"x"))
        with self.assertRaises(FileNotFoundError):
            storage.open("audits/0/missing.png")


class ThumbSrcFilterTests(TestCase):
    def test_review_storage_gives_thumb_url_and_disk_storage_gives_url(self):
        from django.core.files.storage import FileSystemStorage
        from django.db.models.fields.files import FieldFile

        from audits.models import AuditScreenshot
        from audits.templatetags.media_tags import thumb_src

        field = AuditScreenshot._meta.get_field("image")
        db_file = FieldFile(None, field, "audits/9/cbw.png")
        db_file.storage = DatabaseStorage()
        self.assertEqual(thumb_src(db_file), "/media-db/audits/9/cbw.png?thumb=1")
        disk_file = FieldFile(None, field, "audits/9/cbw.png")
        disk_file.storage = FileSystemStorage(base_url="/media/")
        self.assertEqual(thumb_src(disk_file), "/media/audits/9/cbw.png")
        self.assertEqual(thumb_src(None), "")
