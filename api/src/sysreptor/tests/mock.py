import contextlib
import random
from datetime import datetime, timedelta
from unittest import mock
from uuid import uuid4

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.module_loading import import_string
from rest_framework.test import APIClient

from sysreptor import signals as sysreptor_signals
from sysreptor.api_utils.models import LanguageToolIgnoreWords
from sysreptor.conf.asgi import application
from sysreptor.pentests.fielddefinition.predefined_fields import (
    finding_fields_default,
    report_sections_default,
)
from sysreptor.pentests.import_export.serializers import RelatedUserDataExportImportSerializer
from sysreptor.pentests.models import (
    ArchivedProject,
    ArchivedProjectKeyPart,
    ArchivedProjectPublicKeyEncryptedKeyPart,
    Comment,
    FindingTemplate,
    FindingTemplateTranslation,
    Language,
    PentestFinding,
    PentestProject,
    ProjectMemberInfo,
    ProjectMemberRole,
    ProjectNotebookPage,
    ProjectType,
    ReviewStatus,
    ShareInfo,
    UploadedAsset,
    UploadedImage,
    UploadedProjectFile,
    UploadedTemplateImage,
    UploadedUserNotebookFile,
    UploadedUserNotebookImage,
    UserNotebookPage,
    UserPublicKey,
)
from sysreptor.pentests.models.project import CommentAnswer, ReportSection
from sysreptor.users.models import APIToken, MFAMethod, PentestUser
from sysreptor.utils import crypto
from sysreptor.utils.configuration import configuration
from sysreptor.utils.fielddefinition.utils import (
    HandleUndefinedFieldsOptions,
    ensure_defined_structure,
    get_field_value_and_definition,
)
from sysreptor.utils.history import history_context


def create_png_file() -> bytes:
    # 1x1 pixel PNG file
    # Source: https://commons.wikimedia.org/wiki/File:1x1.png
    return b'\x89PNG\r\n\x1a\n\x00\x00\x00\r' + \
           b'IHDR\x00\x00\x00\x01\x00\x00\x00\x01\x01\x03\x00\x00\x00%\xdbV\xca\x00\x00\x00\x03' + \
           b'PLTE\x00\x00\x00\xa7z=\xda\x00\x00\x00\x01tRNS\x00@\xe6\xd8f\x00\x00\x00\n' + \
           b'IDAT\x08\xd7c`\x00\x00\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82'


def create_user(mfa=False, apitoken=False, public_key=False, notes_kwargs=None, images_kwargs=None, files_kwargs=None, **kwargs) -> PentestUser:
    username = f'user_{get_random_string(8)}'
    user = PentestUser.objects.create_user(**{
        'username': username,
        'password': None,
        'email': username + '@example.com',
        'first_name': 'Herbert',
        'last_name': 'Testinger',
    } | kwargs)
    if mfa:
        MFAMethod.objects.create_totp(user=user, is_primary=True)
        MFAMethod.objects.create_backup(user=user)
    if apitoken:
        APIToken.objects.create(user=user, name=f'API token {username}')
    if public_key:
        create_public_key(user=user)

    for note_kwargs in notes_kwargs if notes_kwargs is not None else [{}]:
        create_usernotebookpage(user=user, **note_kwargs)
    for idx, image_kwargs in enumerate(images_kwargs if images_kwargs is not None else [{}]):
        UploadedUserNotebookImage.objects.create(linked_object=user, **{
            'name': f'file{idx}.png',
            'file': SimpleUploadedFile(name=f'file{idx}.png', content=create_png_file()),
        } | image_kwargs)
    for idx, file_kwargs in enumerate(files_kwargs if files_kwargs is not None else [{}]):
        UploadedUserNotebookFile.objects.create(linked_object=user, **{
            'name': f'file{idx}.pdf',
            'file': SimpleUploadedFile(name=f'file{idx}.pdf', content=f'%PDF-1.3{idx}'.encode()),
        } | file_kwargs)

    return user


def create_imported_member(roles=None, **kwargs):
    username = f'user_{get_random_string(8)}'
    return RelatedUserDataExportImportSerializer(instance=ProjectMemberInfo(
        user=PentestUser(**{
            'username': username,
            'email': f'{username}@example.com',
            'first_name': 'Imported',
            'last_name': 'User',
        } | kwargs),
        roles=roles if roles is not None else ProjectMemberRole.default_roles)).data


@history_context()
def create_template(translations_kwargs=None, images_kwargs=None, **kwargs) -> FindingTemplate:
    data = {
        'title': f'Finding Template #{get_random_string(8)}',
        'description': 'Template Description ![](/images/name/file0.png)' if images_kwargs is None else 'Template Description',
        'recommendation': 'Template Recommendation',
        'unknown_field': 'test',
    } | kwargs.pop('data', {})
    language = kwargs.pop('language', Language.ENGLISH_US)
    status = kwargs.pop('status', ReviewStatus.IN_PROGRESS)

    template = FindingTemplate(**{
        'tags': ['web', 'dev'],
        'skip_post_create_signal': True,
    } | kwargs)
    template.save_without_historical_record()

    main_translation = FindingTemplateTranslation(template=template, language=language, status=status)
    main_translation.update_data(data)
    main_translation.save()

    template.main_translation = main_translation
    template._history_type = '+'
    template.save()
    del template._history_type

    for translation_kwargs in (translations_kwargs or []):
        create_template_translation(template=template, **translation_kwargs)

    for idx, image_kwargs in enumerate(images_kwargs if images_kwargs is not None else [{}]):
        UploadedTemplateImage.objects.create(linked_object=template, **{
            'name': f'file{idx}.png',
            'file': SimpleUploadedFile(name=f'file{idx}.png', content=create_png_file()),
        } | image_kwargs)

    sysreptor_signals.post_create.send(sender=template.__class__, instance=template)

    return template


def create_template_translation(template, **kwargs):
    translation_data = {
        'title': 'Finding Template Translation',
    } | kwargs.pop('data', {})
    translation = FindingTemplateTranslation(template=template, **kwargs)
    translation.update_data(translation_data)
    translation.save()
    return translation


def create_project_type(assets_kwargs=None, **kwargs) -> ProjectType:
    additional_fields_simple = [
        {'id': 'field_string', 'type': 'string', 'label': 'String Field', 'default': 'test'},
        {'id': 'field_markdown', 'type': 'markdown', 'label': 'Markdown Field', 'default': '# test\nmarkdown'},
        {'id': 'field_cvss', 'type': 'cvss', 'label': 'CVSS Field', 'default': 'n/a'},
        {'id': 'field_cwe', 'type': 'cwe', 'label': 'CWE Field', 'default': 'CWE-89'},
        {'id': 'field_date', 'type': 'date', 'label': 'Date Field', 'default': '2022-01-01'},
        {'id': 'field_int', 'type': 'number', 'label': 'Number Field', 'default': 10},
        {'id': 'field_bool', 'type': 'boolean', 'label': 'Boolean Field', 'default': False},
        {'id': 'field_enum', 'type': 'enum', 'label': 'Enum Field', 'choices': [{'value': 'enum1', 'label': 'Enum Value 1'}, {'value': 'enum2', 'label': 'Enum Value 2'}], 'default': 'enum2'},
        {'id': 'field_combobox', 'type': 'combobox', 'label': 'Combobox Field', 'suggestions': ['value 1', 'value 2'], 'default': 'value1'},
        {'id': 'field_user', 'type': 'user', 'label': 'User Field'},
    ]
    additional_fields = additional_fields_simple + [
        {'id': 'field_object', 'type': 'object', 'label': 'Nested Object', 'properties': sorted([{'id': 'nested1', 'type': 'string', 'label': 'Nested Field'}] + additional_fields_simple, key=lambda f: f['id'])},
        {'id': 'field_list', 'type': 'list', 'label': 'List Field', 'items': {'type': 'string'}},
        {'id': 'field_list_objects', 'type': 'list', 'label': 'List of nested objects', 'items': {'type': 'object', 'properties': sorted([{'id': 'nested1', 'type': 'string', 'label': 'Nested object field', 'default': None}] + additional_fields_simple, key=lambda f: f['id'])}},
    ]
    report_sections = report_sections_default()
    next(s for s in report_sections if s['id'] == 'other')['fields'] += additional_fields

    project_type = ProjectType.objects.create(**{
        'name': f'Project Type #{get_random_string(8)}',
        'language': Language.ENGLISH_US,
        'status': ReviewStatus.FINISHED,
        'tags': ['web', 'example'],
        'report_sections': report_sections,
        'finding_fields': finding_fields_default() + additional_fields,
        'default_notes': [
            {'id': str(uuid4()), 'parent': None, 'order': 1, 'checked': None, 'icon_emoji': '🦖', 'title': 'Default note 1', 'text': 'Default note 1 text'},
        ],
        'report_template': '''<section><h1>{{ report.title }}</h1></section><section v-for="finding in findings"><h2>{{ finding.title }}</h2></section>''',
        'report_styles': '''@page { size: A4 portrait; } h1 { font-size: 3em; font-weight: bold; }''',
        'report_preview_data': {
            'report': {'title': 'Demo Report', 'field_string': 'test', 'field_int': 5, 'unknown_field': 'test'},
            'findings': [{'title': 'Demo finding', 'unknown_field': 'test'}],
        },
        'skip_post_create_signal': True,
    } | kwargs)
    for idx, asset_kwargs in enumerate(assets_kwargs if assets_kwargs is not None else [{}] * 1):
        UploadedAsset.objects.create(linked_object=project_type, **{
            'name': f'file{idx}.png',
            'file': SimpleUploadedFile(name=f'file{idx}.png', content=asset_kwargs.pop('content', create_png_file())),
        } | asset_kwargs)

    UploadedAsset.objects.create(linked_object=project_type, name='file1.png', file=SimpleUploadedFile(name='file1.png', content=b'file1'))
    UploadedAsset.objects.create(linked_object=project_type, name='file2.png', file=SimpleUploadedFile(name='file2.png', content=b'file2'))

    sysreptor_signals.post_create.send(sender=project_type.__class__, instance=project_type)

    return project_type


def create_comment(finding=None, section=None, user=None, path=None, text_range=None, text_original=None, answers_kwargs=None, **kwargs) -> Comment:
    if path and text_range and text_original is None:
        obj = finding or section
        _, value, _ = get_field_value_and_definition(data=obj.data, definition=obj.field_definition, path=path.split('.')[1:])
        text_original = value[text_range.from_:text_range.to]

    comment = Comment.objects.create(**{
        'text': 'Comment text',
        'finding': finding,
        'section': section,
        'user': user,
        'path': path or 'data.title',
        'text_range': text_range,
        'text_original': text_original,
    } | kwargs)

    for answer_kwargs in (answers_kwargs if answers_kwargs is not None else [{}] * 1):
        CommentAnswer.objects.create(comment=comment, **{
            'text': 'Answer text',
            'user': user,
        } | answer_kwargs)

    return comment


def create_finding(project, template=None, **kwargs) -> PentestFinding:
    data = ensure_defined_structure(
        value={
            'title': f'Finding #{get_random_string(8)}',
            'cvss': 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H',
            'description': 'Finding Description',
            'recommendation': 'Finding Recommendation',
            'unknown_field': 'test',
        } | (template.main_translation.data if template else {}),
        definition=project.project_type.finding_fields_obj,
        handle_undefined=HandleUndefinedFieldsOptions.FILL_DEFAULT,
        include_unknown=True,
    ) | kwargs.pop('data', {})
    return PentestFinding.objects.create(**{
        'project': project,
        'assignee': None,
        'template_id': template.id if template else None,
        'data': data,
    } | kwargs)


def create_usernotebookpage(**kwargs) -> UserNotebookPage:
    return UserNotebookPage.objects.create(**{
        'title': f'Note #{get_random_string(8)}',
        'text': 'Note text',
        'checked': random.choice([None, True, False]),  # noqa: S311
        'icon_emoji': random.choice([None, '🦖']),  # noqa: S311,
    } | kwargs)


def create_projectnotebookpage(**kwargs) -> ProjectNotebookPage:
    return ProjectNotebookPage.objects.create(**{
        'title': f'Note #{get_random_string(8)}',
        'text': 'Note text',
        'checked': random.choice([None, True, False]),  # noqa: S311
        'icon_emoji': random.choice([None, '🦖']),  # noqa: S311,
    } | kwargs)


def create_shareinfo(note, **kwargs):
    return ShareInfo.objects.create(**{
        'note': note,
        'expire_date': (timezone.now() + timedelta(days=30)).date(),
        'permissions_write': True,
    } | kwargs)


def create_project(project_type=None, members=None, report_data=None, findings_kwargs=None, notes_kwargs=None, images_kwargs=None, files_kwargs=None, comments=False, **kwargs) -> PentestProject:
    project_type = project_type or create_project_type()
    report_data = {
        'title': 'Report title',
        'unknown_field': 'test',
        'field_markdown': '![](/images/name/file0.png) [](/files/name/file0.pdf)' if images_kwargs is None and files_kwargs is None else 'test',
    } | (report_data or {})
    project = PentestProject.objects.create(**{
        'project_type': project_type,
        'name': f'Pentest Project #{get_random_string(8)}',
        'language': Language.ENGLISH_US,
        'tags': ['web', 'customer:test'],
        'unknown_custom_fields': {f: report_data.pop(f) for f in set(report_data.keys()) - set(project_type.all_report_fields_obj.keys())},
        'skip_post_create_signal': True,
    } | kwargs)

    sections = project.sections.all()
    section_histories = list(ReportSection.history.filter(project_id=project))
    for s in sections:
        s.update_data({f: v for f, v in report_data.items() if f in s.field_definition})
        if sh := next(filter(lambda sh: sh.section_id == s.section_id, section_histories), None):
            sh.custom_fields = s.custom_fields
    ReportSection.objects.bulk_update(sections, ['custom_fields'])
    ReportSection.history.bulk_update(section_histories, ['custom_fields'])

    member_infos = []
    for m in (members or []):
        if isinstance(m, PentestUser):
            member_infos.append(ProjectMemberInfo(project=project, user=m, roles=ProjectMemberRole.default_roles))
        elif isinstance(m, ProjectMemberInfo):
            m.project = project
            member_infos.append(m)
        else:
            raise ValueError('Unsupported member type')
    project.set_members(member_infos, new=True)

    comment_user = member_infos[0].user if member_infos else None
    if comments:
        for section in project.sections.all():
            create_comment(section=section, user=comment_user, path='data.' + list(section.field_definition.keys())[0])

    for finding_kwargs in findings_kwargs if findings_kwargs is not None else [{}] * 1:
        finding = create_finding(project=project, **finding_kwargs)
        if comments:
            create_comment(finding=finding, user=comment_user, path='data.title')

    if notes_kwargs is not None:
        # Delete default notes
        project.notes.all().delete()
    for note_kwargs in notes_kwargs if notes_kwargs is not None else [{}] * 1:
        create_projectnotebookpage(project=project, **note_kwargs)

    for idx, image_kwargs in enumerate(images_kwargs if images_kwargs is not None else [{}] * 1):
        UploadedImage.objects.create(linked_object=project, **{
            'name': f'file{idx}.png',
            'file': SimpleUploadedFile(name=f'file{idx}.png', content=image_kwargs.pop('content', create_png_file())),
        } | image_kwargs)
    for idx, file_kwargs in enumerate(files_kwargs if files_kwargs is not None else [{}] * 1):
        UploadedProjectFile.objects.create(linked_object=project, **{
            'name': f'file{idx}.pdf',
            'file': SimpleUploadedFile(name=f'file{idx}.pdf', content=file_kwargs.pop('content', f'%PDF-1.3{idx}'.encode())),
        } | file_kwargs)

    sysreptor_signals.post_create.send(sender=PentestProject, instance=project)

    return project


def create_public_key(**kwargs):
    dummy_data = {
        'name': f'Public key #{get_random_string(8)}',
    }
    if 'public_key' not in kwargs:
        dummy_data |= {
            'public_key':
                '-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n' +
                'mDMEZBryexYJKwYBBAHaRw8BAQdAI2A6jJCXSGP10s2H1duX22saF2lX4CtGzX+H\n' +
                'xm4nN8W0LEF1dG9nZW5lcmF0ZWQgS2V5IDx1bnNwZWNpZmllZEA3MmNmMGYzYTc4\n' +
                'NmQ+iJAEExYIADgWIQTC5xEj3lvM80ruTt39spmRS6kHgwUCZBryewIbIwULCQgH\n' +
                'AgYVCgkICwIEFgIDAQIeAQIXgAAKCRD9spmRS6kHgxspAQDrxnxj2eRaubEX547n\n' +
                'w+wE1PJohJqLoWERuCz2UuJLRwEA44NZVlPHdkwUXeP7otuOeA0ZCzOQIc+/60Pr\n' +
                'aeqVEQi4cwRkGvJ7EgUrgQQAIgMDBHlYyMT98UVGIaFUu2p/rkbOGnZ1k5d/KtMx\n' +
                '8TxqyU1cpdIzTvOVD4ykunTzsWsi60ERcNg6vDuHcDCapHYmvuk/+g49NQFNutRX\n' +
                'fnNxVj091cH3ioJCgQ1wbYgoW0qfCQMBCQiIeAQYFggAIBYhBMLnESPeW8zzSu5O\n' +
                '3f2ymZFLqQeDBQJkGvJ7AhsMAAoJEP2ymZFLqQeDrOUBAKnrakgp/dYWsMIHwiAg\n' +
                'Nq1F1YAX92oNteAVpTRNkwyIAQC68j1ytjpdoEbYlAPfQtKljjDSDONLxmmZWPxP\n' +
                'Ya8sAg==\n' +
                '=jbm4\n' +
                '-----END PGP PUBLIC KEY BLOCK-----\n',
            'public_key_info': {
                'cap': 'scaESCA',
                'algo': '22',
                'type': 'pub',
                'curve': 'ed25519',
                'subkey_info': {
                    'C3B01D1054571D18': {
                        'cap': 'e',
                        'algo': '18',
                        'type': 'sub',
                        'curve': 'nistp384',
                    },
                },
            },
        }

    return UserPublicKey.objects.create(**dummy_data | kwargs)


def create_archived_project(project=None, **kwargs):
    name = project.name if project else f'Archive #{get_random_string(8)}'
    users = [m.user for m in project.members.all()] if project else [create_user(public_key=True)]

    archive = ArchivedProject.objects.create(name=name, threshold=1, file=SimpleUploadedFile('archive.tar.gz', crypto.MAGIC + b'dummy-data'))
    key_parts = []
    encrypted_key_parts = []
    for u in users:
        key_parts.append(ArchivedProjectKeyPart(archived_project=archive, user=u, encrypted_key_part=b'dummy-data'))
        for pk in u.public_keys.all():
            encrypted_key_parts.append(ArchivedProjectPublicKeyEncryptedKeyPart(key_part=key_parts[-1], public_key=pk, encrypted_data='dummy-data'))

    if not encrypted_key_parts:
        raise ValueError('No public keys set for users')
    ArchivedProjectKeyPart.objects.bulk_create(key_parts)
    ArchivedProjectPublicKeyEncryptedKeyPart.objects.bulk_create(encrypted_key_parts)
    return archive


def create_languagetool_ignore_word(user=None, ignore_word='test'):
    return LanguageToolIgnoreWords.objects.create(
        user_id=user.id.int >> 65 if user else 1,
        ignore_word=ignore_word,
        created_at=timezone.now(),
        updated_at=timezone.now(),
    )


def mock_time(before=None, after=None):
    def now():
        return datetime.now(tz=timezone.get_current_timezone()) - (before or timedelta()) + (after or timedelta())
    return mock.patch('django.utils.timezone.now', now)


def api_client(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


def create_session(user):
    engine = import_string(settings.SESSION_ENGINE)
    session = engine.SessionStore()
    if user and not user.is_anonymous:
        session[SESSION_KEY] = str(user.id)
        session[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
        session[HASH_SESSION_KEY] = user.get_session_auth_hash()
        session['admin_permissions_enabled'] = True
    session.save()
    return session


@contextlib.asynccontextmanager
async def websocket_client(path, user, connect=True):
    session = await sync_to_async(create_session)(user)
    consumer = WebsocketCommunicator(
        application=application,
        path=path,
        headers=[(b'cookie', f'{settings.SESSION_COOKIE_NAME}={session.session_key}'.encode())],
    )
    consumer.session = session

    try:
        # Connect
        if connect:
            connected, _ = await consumer.connect()
            assert connected

        yield consumer
    finally:
        await consumer.disconnect()


@contextlib.contextmanager
def override_configuration(**kwargs):
    restore_map = configuration._force_override.copy()
    try:
        configuration._force_override |= kwargs
        configuration.clear_cache()
        yield
    finally:
        configuration._force_override = restore_map
        configuration.clear_cache()


def update(obj, **kwargs):
    for k, v in kwargs.items():
        if k == 'data' and hasattr(obj, 'update_data'):
            obj.update_data(v)
        else:
            setattr(obj, k, v)
    obj.save()
    return obj
