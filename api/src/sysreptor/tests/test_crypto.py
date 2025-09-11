import base64
import io
import json
import random
import tempfile
from contextlib import contextmanager
from uuid import UUID

import pytest
from django.conf import settings
from django.core import management
from django.core.files.storage import FileSystemStorage, storages
from django.db import connection
from django.test import override_settings
from django.urls import reverse

from sysreptor.management.commands import encryptdata
from sysreptor.pentests.models import (
    ArchivedProject,
    PentestFinding,
    PentestProject,
    ProjectNotebookExcalidrawFile,
    ProjectType,
    UploadedAsset,
    UploadedImage,
    UploadedProjectFile,
    UserPublicKey,
)
from sysreptor.tests.mock import (
    api_client,
    create_archived_project,
    create_project,
    create_public_key,
    create_template,
    create_user,
    override_configuration,
    update,
)
from sysreptor.utils import crypto
from sysreptor.utils.crypto import pgp
from sysreptor.utils.storages import EncryptedFileSystemStorage


def assert_db_field_encrypted(query, expected):
    with connection.cursor() as cursor:
        cursor.execute(*query.query.as_sql(compiler=query.query.compiler, connection=connection))
        row = cursor.fetchone()
    assert row[0].startswith(crypto.MAGIC) == expected


def assert_storage_file_encrypted(file, expected):
    with file.open(mode='rb').file.file.fileobj as f:
        f.seek(0)
        assert f.read().startswith(crypto.MAGIC) == expected


class TestSymmetricEncryptionTests:
    @pytest.fixture(autouse=True)
    def setUp(self):
        self.key = crypto.EncryptionKey(id='test-key', key=b'a' * (256 // 8))
        self.nonce = b'n' * 16
        self.plaintext = b'This is a plaintext content which will be encrypted in unit tests. ' + (b'a' * 100) + b' lorem impsum long text'

        with override_settings(ENCRYPTION_PLAINTEXT_FALLBACK=True):
            yield

    def encrypt(self, pt):
        enc = io.BytesIO()
        with crypto.open(fileobj=enc, mode='w', key=self.key, nonce=self.nonce) as c:
            c.write(pt)
        return enc.getvalue()

    @contextmanager
    def open_decrypt(self, ct, **kwargs):
        with crypto.open(fileobj=io.BytesIO(ct), mode='r', keys={self.key.id: self.key}, **kwargs) as c:
            yield c

    def decrypt(self, ct, **kwargs):
        with self.open_decrypt(ct, **kwargs) as c:
            return c.read()

    def modify_metadata(self, enc, m):
        ct_start_index = enc.index(b'\x00')
        metadata = json.loads(enc[len(crypto.MAGIC):ct_start_index].decode())
        metadata |= m
        return crypto.MAGIC + json.dumps(metadata).encode() + enc[ct_start_index:]

    def test_encryption_decryption(self):
        enc = self.encrypt(self.plaintext)
        assert enc.startswith(crypto.MAGIC)
        dec = self.decrypt(enc)
        assert dec == self.plaintext

    def test_encryption_chunked(self):
        enc = io.BytesIO()
        with crypto.open(enc, 'w', key=self.key, nonce=self.nonce) as c:
            for b in self.plaintext:
                c.write(bytes([b]))
        assert enc.getvalue() == self.encrypt(self.plaintext)
        assert self.decrypt(enc.getvalue()) == self.plaintext

    def test_decryptions_chunked(self):
        dec = b''
        with self.open_decrypt(self.encrypt(self.plaintext)) as c:
            while b := c.read(1):
                dec += b
        assert dec == self.plaintext

    def test_read_plaintext(self):
        dec = self.decrypt(self.plaintext)
        assert dec == self.plaintext

    def test_write_plaintext(self):
        enc = io.BytesIO()
        with crypto.open(enc, mode='w', key=None) as c:
            c.write(self.plaintext)
        assert enc.getvalue() == self.plaintext

    def test_verify_payload(self):
        enc = bytearray(self.encrypt(self.plaintext))
        enc[100] = (enc[100] + 10) & 0xFF  # Modify ciphertext
        with pytest.raises(crypto.CryptoError):
            self.decrypt(enc)

    def test_verify_header(self):
        enc = self.encrypt(self.plaintext)
        modified = self.modify_metadata(enc, {'added_field': 'new'})
        with pytest.raises(crypto.CryptoError):
            self.decrypt(modified)

    def test_verify_key(self):
        enc = self.encrypt(self.plaintext)
        modified = self.modify_metadata(enc, {'nonce': base64.b64encode(b'x' * 16).decode()})
        with pytest.raises(crypto.CryptoError):
            self.decrypt(modified)

    def test_missing_metadata(self):
        enc = self.encrypt(self.plaintext)[:10]
        with pytest.raises(crypto.CryptoError):
            self.decrypt(enc)

    def test_corrupted_magic(self):
        enc = self.encrypt(self.plaintext)
        enc = b'\x00\x00' + enc[2:]
        assert self.decrypt(enc) == enc

    def test_partial_magic(self):
        enc = crypto.MAGIC[2:]
        assert self.decrypt(enc) == enc

    def test_missing_tag(self):
        enc = self.encrypt(self.plaintext)
        enc = enc[:enc.index(b'\x00') + 3]
        with pytest.raises(crypto.CryptoError):
            self.decrypt(enc)

    def test_encrypt_empty(self):
        enc = self.encrypt(b'')
        assert enc.startswith(crypto.MAGIC)
        assert self.decrypt(enc) == b''

    def test_decryption_seek(self):
        enc = self.encrypt(self.plaintext)
        with self.open_decrypt(enc) as c:
            c.seek(20, io.SEEK_SET)
            assert c.tell() == 20
            assert c.read(5) == self.plaintext[20:25]
            assert c.tell() == 25

            assert c.seek(0, io.SEEK_CUR) == 25
            assert c.tell() == 25
            assert c.read(5) == self.plaintext[25:30]

            c.seek(0, io.SEEK_END)
            assert c.tell() == len(self.plaintext)
            assert c.read(5) == b''

            c.seek(c.tell() - 5, io.SEEK_SET)
            assert c.tell() == len(self.plaintext) - 5
            assert c.read(5) == self.plaintext[-5:]

            c.seek(0, io.SEEK_SET)
            assert c.tell() == 0
            assert c.read(5) == self.plaintext[:5]

    def test_encrypt_revoked_key(self):
        self.key.revoked = True
        with pytest.raises(crypto.CryptoError):
            self.encrypt(self.plaintext)

    def test_decrypt_revoked_key(self):
        enc = self.encrypt(self.plaintext)
        self.key.revoked = True
        with pytest.raises(crypto.CryptoError):
            self.decrypt(enc)

    def test_plaintext_fallback_disabled_encryption(self):
        with pytest.raises(crypto.CryptoError):
            with crypto.open(fileobj=io.BytesIO(), mode='w', key=None, plaintext_fallback=False) as c:
                c.write(self.plaintext)

    def test_plaintext_fallback_disabled_decryption(self):
        with pytest.raises(crypto.CryptoError):
            self.decrypt(self.plaintext, plaintext_fallback=False)


class TestEncryptedStorage:
    @pytest.fixture(autouse=True)
    def setUp(self):
        with tempfile.TemporaryDirectory() as location:
            self.storage_plain = FileSystemStorage(location=location)
            self.storage_crypto = EncryptedFileSystemStorage(location=location)
            self.plaintext = b'This is a test file content which should be encrypted'

            with override_settings(
                ENCRYPTION_KEYS={'test-key': crypto.EncryptionKey(id='test-key', key=b'a' * 32)},
                DEFAULT_ENCRYPTION_KEY_ID='test-key',
                ENCRYPTION_PLAINTEXT_FALLBACK=True,
            ):
                yield

    def test_save(self):
        filename = self.storage_crypto.save('test.txt', io.BytesIO(self.plaintext))
        assert str(UUID(filename.replace('/', ''))) != 'test.txt'
        enc = self.storage_plain.open(filename, mode='rb').read()
        assert enc.startswith(crypto.MAGIC)
        dec = self.storage_crypto.open(filename, mode='rb').read()
        assert dec == self.plaintext

    def test_open(self):
        filename = self.storage_crypto.save('test.txt', io.BytesIO(b''))
        with self.storage_crypto.open(filename, mode='wb') as f:
            f.write(self.plaintext)

        enc = self.storage_plain.open(filename, mode='rb').read()
        assert enc.startswith(crypto.MAGIC)
        dec = self.storage_crypto.open(filename, mode='rb').read()
        assert dec == self.plaintext

    def test_size(self):
        filename = self.storage_crypto.save('test.txt', io.BytesIO(self.plaintext))
        assert self.storage_crypto.size(filename) == len(self.storage_crypto.open(filename, mode='rb').read())


@pytest.mark.django_db()
class TestEncryptedDbField:
    @pytest.fixture(autouse=True)
    def setUp(self):
        self.template = create_template()
        project = create_project()
        self.finding = project.findings.first()
        self.user = create_user()

        with override_settings(
            ENCRYPTION_KEYS={'test-key': crypto.EncryptionKey(id='test-key', key=b'a' * 32)},
            DEFAULT_ENCRYPTION_KEY_ID='test-key',
            ENCRYPTION_PLAINTEXT_FALLBACK=True,
        ):
            yield

    def test_transparent_encryption(self):
        # Test transparent encryption/decryption. No encrypted data should be returned to caller
        data_dict = {'test': 'content'}
        update(self.finding, custom_fields=data_dict, template_id=self.template.id)
        self.user.set_password('pwd')
        self.user.save()

        assert_db_field_encrypted(PentestFinding.objects.filter(id=self.finding.id).values('custom_fields'), True)

        f = PentestFinding.objects.filter(id=self.finding.id).get()
        assert f.custom_fields == data_dict
        assert f.template_id == self.template.id
        assert self.user.check_password('pwd')

    def test_data_stored_encrypted(self):
        update(self.finding, custom_fields={'test': 'content'}, template_id=self.template.id)
        assert_db_field_encrypted(PentestFinding.objects.filter(id=self.finding.id).values('custom_fields'), True)

    @override_settings(DEFAULT_ENCRYPTION_KEY_ID=None)
    def test_db_encryption_disabled(self):
        update(self.finding, custom_fields={'test': 'content'}, template_id=self.template.id)
        assert_db_field_encrypted(PentestFinding.objects.filter(id=self.finding.id).values('custom_fields'), False)


@pytest.mark.django_db()
class TestEncryptDataCommand:
    @pytest.fixture(autouse=True)
    def setUp(self):
        with override_settings(
            ENCRYPTION_KEYS={},
            DEFAULT_ENCRYPTION_KEY_ID=None,
            ENCRYPTION_PLAINTEXT_FALLBACK=True,
            STORAGES=settings.STORAGES | {
                'uploadedimages': {'BACKEND': 'sysreptor.utils.storages.EncryptedInMemoryStorage'},
                'uploadedassets': {'BACKEND': 'sysreptor.utils.storages.EncryptedInMemoryStorage'},
                'uploadedfiles': {'BACKEND': 'sysreptor.utils.storages.EncryptedInMemoryStorage'},
            },
        ):
            UploadedImage.file.field.storage = storages['uploadedimages']
            UploadedAsset.file.field.storage = storages['uploadedassets']
            UploadedProjectFile.file.field.storage = storages['uploadedfiles']
            ProjectNotebookExcalidrawFile.file.field.storage = storages['uploadedfiles']
            self.project = create_project()
            yield

    @override_settings(
        ENCRYPTION_KEYS={'test-key': crypto.EncryptionKey(id='test-key', key=b'a' * 32)},
        DEFAULT_ENCRYPTION_KEY_ID='test-key',
    )
    def test_command(self):
        management.call_command(encryptdata.Command())

        p = PentestProject.objects.filter(id=self.project.id)
        assert_db_field_encrypted(p.values('unknown_custom_fields'), True)
        for i in p.get().images.all():
            assert_db_field_encrypted(UploadedImage.objects.filter(id=i.id).values('name'), True)
            assert_storage_file_encrypted(i.file, True)
        for f in p.get().files.all():
            assert_db_field_encrypted(UploadedProjectFile.objects.filter(id=f.id).values('name'), True)
            assert_storage_file_encrypted(f.file, True)

        pt = ProjectType.objects.filter(id=self.project.project_type.id)
        assert_db_field_encrypted(pt.values('report_template'), True)
        assert_db_field_encrypted(pt.values('report_styles'), True)
        assert_db_field_encrypted(pt.values('report_preview_data'), True)
        for a in pt.get().assets.all():
            assert_db_field_encrypted(UploadedAsset.objects.filter(id=a.id).values('name'), True)
            assert_storage_file_encrypted(a.file, True)


@pytest.mark.django_db()
class TestProjectArchivingEncryption:
    @pytest.fixture(autouse=True)
    def setUp(self):
        with override_configuration(PROJECT_MEMBERS_CAN_ARCHIVE_PROJECTS=True), \
             pgp.create_gpg() as self.gpg:
            yield

    def create_user_with_private_key(self, **kwargs):
        user = create_user(public_key=False, **kwargs)
        master_key = self.gpg.gen_key(self.gpg.gen_key_input(
            key_type='EdDSA',
            key_curve='ed25519',
            no_protection=True,
            subkey_type='ECDH',
            subkey_curve='nistp384',
        ))
        public_key_pem = self.gpg.export_keys(master_key.fingerprint)
        create_public_key(user=user, public_key=public_key_pem)
        return user

    def test_register_public_key(self):
        user = create_user()
        client = api_client(user)

        master_key = self.gpg.gen_key(self.gpg.gen_key_input(
            key_type='EdDSA',
            key_curve='ed25519',
            no_protection=True,
            subkey_type='ECDH',
            subkey_curve='nistp384',
        ))
        public_key_pem = self.gpg.export_keys(master_key.fingerprint)
        res = client.post(reverse('userpublickey-register-begin', kwargs={'pentestuser_pk': 'self'}), data={
            'name': 'Test Public Key',
            'public_key': public_key_pem,
        })
        assert res.status_code == 200
        assert res.data['status'] == 'verify-key'

        verification_decrypted = self.gpg.decrypt(res.data['verification'])
        res = client.post(reverse('userpublickey-register-complete', kwargs={'pentestuser_pk': 'self'}), data={
            'verification': verification_decrypted.data.decode(),
        })
        assert res.status_code == 201
        user_public_key = UserPublicKey.objects.get(id=res.data['id'])
        assert user_public_key.public_key == public_key_pem

    def test_delete_public_key(self):
        user = create_user(public_key=True)
        archive = create_archived_project(project=create_project(members=[user], readonly=True))
        client = api_client(user)

        # public key used in archive
        res1 = client.delete(reverse('userpublickey-detail', kwargs={'pentestuser_pk': 'self', 'pk': user.public_keys.first().id}))
        assert res1.status_code == 400

        # public key not used in archived
        archive.delete()
        res2 = client.delete(reverse('userpublickey-detail', kwargs={'pentestuser_pk': 'self', 'pk': user.public_keys.first().id}))
        assert res2.status_code == 204

    @pytest.mark.parametrize(('expected', 'threshold', 'num_users_with_key', 'num_users_without_key'), [
        (False, 1, 0, 2),  # no users with key
        (False, 2, 1, 2),  # too few users with key
        (False, 5, 3, 0),  # threshold too high
        (True, 2, 3, 1),
    ])
    def test_archiving_validation(self, expected, threshold, num_users_with_key, num_users_without_key):
        with override_configuration(ARCHIVING_THRESHOLD=threshold):
            users = [create_user(public_key=True) for _ in range(num_users_with_key)] + \
                    [create_user(public_key=False) for _ in range(num_users_without_key)]
            project = create_project(members=users, readonly=True)
            res = api_client(users[0]).post(reverse('pentestproject-archive', kwargs={'pk': project.pk}))
            assert (res.status_code == 201) == expected

    @override_configuration(ARCHIVING_THRESHOLD=2)
    def test_archiving_dearchiving(self):
        user_regular = self.create_user_with_private_key()
        user_archiver1 = self.create_user_with_private_key(is_global_archiver=True)
        user_archiver2 = self.create_user_with_private_key(is_global_archiver=True)
        user_without_key = create_user()
        project = create_project(members=[user_regular, user_archiver1, user_without_key], readonly=True)

        client = api_client(user_regular)
        res = client.post(reverse('pentestproject-archive', kwargs={'pk': project.pk}))
        assert res.status_code == 201

        archive = ArchivedProject.objects.get(id=res.data['id'])
        assert archive.threshold == 2
        assert archive.name == project.name
        assert archive.tags == project.tags
        assert archive.key_parts.count() == 3
        assert set(archive.key_parts.values_list('user_id', flat=True)) == {user_regular.id, user_archiver1.id, user_archiver2.id}
        assert not PentestProject.objects.filter(id=project.id).exists()

        # Decrypt first keypart
        keypart1 = archive.key_parts.get(user=user_regular)
        keypart_kwargs1 = {'archivedproject_pk': archive.id, 'pk': keypart1.id}
        res_k1 = client.get(reverse('archivedprojectkeypart-public-key-encrypted-data', kwargs=keypart_kwargs1))
        assert res_k1.status_code == 200
        res_d1 = client.post(reverse('archivedprojectkeypart-decrypt', kwargs=keypart_kwargs1), data={
            'data': self.gpg.decrypt(res_k1.data[0]['encrypted_data']).data.decode().split('\n')[1],
        })
        assert res_d1.status_code == 200
        assert res_d1.data['status'] == 'key-part-decrypted'
        keypart1.refresh_from_db()
        assert keypart1.is_decrypted

        # Decrypt second keypart => restores whole project
        client2 = api_client(user_archiver2)
        keypart2 = archive.key_parts.get(user=user_archiver2)
        keypart_kwargs2 = {'archivedproject_pk': archive.id, 'pk': keypart2.id}
        res_k2 = client2.get(reverse('archivedprojectkeypart-public-key-encrypted-data', kwargs=keypart_kwargs2))
        assert res_k2.status_code == 200
        res_d2 = client2.post(reverse('archivedprojectkeypart-decrypt', kwargs=keypart_kwargs2), data={
            'data': self.gpg.decrypt(res_k2.data[0]['encrypted_data']).data.decode().split('\n')[1],
        })
        assert res_d2.status_code == 200
        assert res_d2.data['status'] == 'project-restored'
        assert not ArchivedProject.objects.filter(id=archive.id).exists()

        project_restored = PentestProject.objects.get(id=res_d2.data['project_id'])
        assert project_restored.name == project.name

    @override_configuration(ARCHIVING_THRESHOLD=1, PROJECT_MEMBERS_CAN_ARCHIVE_PROJECTS=False)
    def test_archiving_disabled_for_project_members(self):
        user_regular = create_user(public_key=True)
        user_archiver = create_user(public_key=True, is_global_archiver=True)
        project = create_project(members=[user_regular, user_archiver], readonly=True)

        # Project member cannot archive
        res = api_client(user_regular).post(reverse('pentestproject-archive', kwargs={'pk': project.pk}))
        assert res.status_code == 403

        # Global archiver can
        res = api_client(user_archiver).post(reverse('pentestproject-archive', kwargs={'pk': project.pk}))
        assert res.status_code == 201

        archive = ArchivedProject.objects.get(id=res.data['id'])
        assert archive.key_parts.count() == 1
        assert archive.key_parts.first().user == user_archiver

    def test_decrypt_wrong_key(self):
        user = create_user(public_key=True)
        archive = create_archived_project(project=create_project(members=[user], readonly=True))

        res = api_client(user).post(reverse('archivedprojectkeypart-decrypt', kwargs={'archivedproject_pk': archive.id, 'pk': archive.key_parts.first().id}), {
            'data': base64.b64encode(random.randbytes(32)).decode(),  # noqa: S311
        })
        assert res.status_code == 400
