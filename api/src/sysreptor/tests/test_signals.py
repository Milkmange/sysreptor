import contextlib

import pytest
from django.urls import reverse
from django.utils.crypto import get_random_string

from sysreptor import signals as sysreptor_signals
from sysreptor.pentests.import_export import (
    export_project_types,
    export_projects,
    export_templates,
    import_project_types,
    import_projects,
    import_templates,
)
from sysreptor.pentests.models import (
    ArchivedProject,
    FindingTemplate,
    PentestProject,
    ProjectType,
    ProjectTypeScope,
)
from sysreptor.pentests.models.project import PentestFinding
from sysreptor.tests.mock import (
    api_client,
    create_finding,
    create_project,
    create_project_type,
    create_template,
    create_user,
    update,
)
from sysreptor.tests.test_import_export import archive_to_file
from sysreptor.users.models import PentestUser
from sysreptor.utils.utils import copy_keys


@pytest.mark.django_db()
class TestSignalsSent:
    @pytest.fixture(autouse=True)
    def setUp(self):
        self.user = create_user(is_designer=True, is_template_editor=True, is_user_manager=True, public_key=True)
        self.client = api_client(self.user)

    @contextlib.contextmanager
    def assert_signal(self, signal, sender=None, expected_call_count=1, expected_subresources=None, signal_handler=None):
        actual_call_count = {'value': 0}

        def signal_handler_func(sender, instance=None, *args, **kwargs):
            actual_call_count['value'] += 1
            # assert sub-resources already created
            if instance:
                for k, v in (expected_subresources or {}).items():
                    assert getattr(instance, k).count() == v

            if signal_handler:
                signal_handler(*args, sender=sender, instance=instance, **kwargs)

        signal.connect(signal_handler_func, sender=sender)
        yield
        signal.disconnect(signal_handler_func)

        assert actual_call_count['value'] == expected_call_count

    @contextlib.contextmanager
    def assert_post_create_signal(self, sender, **kwargs):
        with self.assert_signal(sysreptor_signals.post_create, sender, **kwargs):
            yield

    @contextlib.contextmanager
    def assert_post_finish_signal(self, **kwargs):
        with self.assert_signal(sysreptor_signals.post_finish, **kwargs):
            yield

    @contextlib.contextmanager
    def assert_post_export_import_signals(self, sender, **kwargs):
        with self.assert_signal(sysreptor_signals.post_export, sender=sender, **kwargs), \
             self.assert_signal(sysreptor_signals.post_import, sender=sender, **kwargs):
            yield

    def test_post_create_project(self):
        p = create_project(members=[self.user])
        expected_subresources = {
            'members': p.members.count(),
            'sections': p.sections.count(),
            'findings': p.findings.count(),
            'notes': p.notes.count(),
            'images': p.images.count(),
            'files': p.files.count(),
        }

        with self.assert_post_create_signal(sender=PentestProject, expected_subresources=expected_subresources):
            create_project(members=[self.user])
        with self.assert_post_create_signal(sender=PentestProject, expected_subresources=expected_subresources):
            p.copy()
        with self.assert_post_create_signal(sender=PentestProject, expected_subresources=expected_subresources):
            import_projects(archive_to_file(export_projects([p], export_all=True)))
        with self.assert_post_create_signal(sender=PentestProject, expected_subresources={'sections': len(p.project_type.report_sections), 'notes': len(p.project_type.default_notes), 'members': 1}):
            res = self.client.post(reverse('pentestproject-list'), data={'name': 'api', 'project_type': p.project_type.id})
            assert res.status_code == 201, res.data
        with self.assert_post_create_signal(sender=PentestProject, expected_subresources={'sections': len(p.project_type.report_sections), 'notes': len(p.project_type.default_notes)}):
            PentestProject.objects.create(project_type=p.project_type)
        with self.assert_post_create_signal(sender=PentestProject, expected_call_count=0):
            update(p, name='other')

    def test_post_create_projecttype(self):
        pt = create_project_type()
        expected_subresources = {
            'assets': pt.assets.count(),
        }

        with self.assert_post_create_signal(sender=ProjectType, expected_subresources=expected_subresources):
            create_project_type()
        with self.assert_post_create_signal(sender=ProjectType, expected_subresources=expected_subresources):
            pt.copy()
        with self.assert_post_create_signal(sender=ProjectType, expected_subresources=expected_subresources):
            import_project_types(archive_to_file(export_project_types([pt])))
        with self.assert_post_create_signal(sender=ProjectType):
            res = self.client.post(reverse('projecttype-list'), data={'name': 'api', 'scope': ProjectTypeScope.GLOBAL.value})
            assert res.status_code == 201, res.data
        with self.assert_post_create_signal(sender=ProjectType):
            ProjectType.objects.create()
        with self.assert_post_create_signal(sender=ProjectType, expected_call_count=0):
            update(pt, name='other')

    def test_post_create_template(self):
        t = create_template()
        expected_subresources = {
            'translations': t.translations.count(),
            'images': t.images.count(),
        }

        with self.assert_post_create_signal(sender=FindingTemplate, expected_subresources=expected_subresources):
            create_template()
        with self.assert_post_create_signal(sender=FindingTemplate, expected_subresources=expected_subresources):
            t.copy()
        with self.assert_post_create_signal(sender=FindingTemplate, expected_subresources=expected_subresources):
            import_templates(archive_to_file(export_templates([t])))
        with self.assert_post_create_signal(sender=FindingTemplate, expected_subresources=copy_keys(expected_subresources, ['translations'])):
            res = self.client.post(reverse('findingtemplate-list'), data=self.client.get(reverse('findingtemplate-detail', kwargs={'pk': t.id})).data)
            assert res.status_code == 201, res.data
        with self.assert_post_create_signal(sender=FindingTemplate):
            FindingTemplate.objects.create()
        with self.assert_post_create_signal(sender=FindingTemplate, expected_call_count=0):
            update(t, tags=['other'])

    def test_post_create_finding(self):
        p = create_project(members=[self.user])

        with self.assert_post_create_signal(sender=PentestFinding):
            f = create_finding(project=p)
        with self.assert_post_create_signal(sender=PentestFinding):
            res = self.client.post(
                path=reverse('finding-list', kwargs={'project_pk': p.id}),
                data=self.client.get(reverse('finding-detail', kwargs={'project_pk': p.id, 'id': f.finding_id})).data,
            )
            assert res.status_code == 201, res.data
        with self.assert_post_create_signal(sender=PentestFinding):
            PentestFinding.objects.create(project=p)
        with self.assert_post_create_signal(sender=PentestFinding, expected_call_count=p.findings.count()):
            import_projects(archive_to_file(export_projects([p])))
        with self.assert_post_create_signal(sender=PentestFinding, expected_call_count=0):
            update(f, data={'title': 'other'})

    def test_post_create_user(self):
        with self.assert_post_create_signal(sender=PentestUser):
            create_user()
        with self.assert_post_create_signal(sender=PentestUser):
            PentestUser.objects.create(username='new1')
        with self.assert_post_create_signal(sender=PentestUser):
            res = self.client.post(reverse('pentestuser-list'), data={'username': 'new2', 'password': get_random_string(32)})
            assert res.status_code == 201, res.data

    def test_post_archive(self):
        def signal_handler(sender, instance, archive, *args, **kwargs):
            assert PentestProject.objects.filter(pk=instance.pk).exists()
            assert ArchivedProject.objects.filter(pk=archive.pk).exists()

        p = create_project(members=[self.user], readonly=True)
        with self.assert_signal(sysreptor_signals.post_archive, signal_handler=signal_handler):
            res = self.client.post(reverse('pentestproject-archive', kwargs={'pk': p.id}))
            assert res.status_code == 201, res.data

    def test_post_finish(self):
        p = create_project(members=[self.user])

        with self.assert_post_finish_signal():
            update(p, readonly=True)

        with self.assert_post_finish_signal(expected_call_count=0):
            update(p, readonly=False)

        with self.assert_post_finish_signal(expected_call_count=0):
            create_project(readonly=True)

    def test_export_import(self):
        with self.assert_post_export_import_signals(sender=PentestProject):
            import_projects(archive_to_file(export_projects([create_project()])))
        with self.assert_post_export_import_signals(sender=ProjectType):
            import_project_types(archive_to_file(export_project_types([create_project_type()])))
        with self.assert_post_export_import_signals(sender=FindingTemplate):
            import_templates(archive_to_file(export_templates([create_template()])))

