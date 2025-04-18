import contextlib
from datetime import timedelta
from unittest import mock
from uuid import uuid4

import pytest
from asgiref.sync import async_to_sync
from django.test import override_settings
from django.utils import timezone
from pytest_django.asserts import assertNumQueries

from sysreptor.pentests.models import ArchivedProject, CollabEvent, CollabEventType, PentestProject
from sysreptor.pentests.tasks import (
    automatically_archive_projects,
    automatically_delete_archived_projects,
    cleanup_collab_events,
    cleanup_project_files,
    cleanup_template_files,
    cleanup_usernotebook_files,
    reset_stale_archive_restores,
)
from sysreptor.tasks.models import (
    PeriodicTask,
    PeriodicTaskInfo,
    PeriodicTaskSpec,
    TaskStatus,
    periodic_task_registry,
)
from sysreptor.tests.mock import (
    create_archived_project,
    create_project,
    create_template,
    create_user,
    mock_time,
    override_configuration,
    update,
)


@contextlib.contextmanager
def override_periodic_tasks(tasks):
    tasks_restore = periodic_task_registry.tasks
    try:
        periodic_task_registry.tasks = set(tasks)
        yield
    finally:
        periodic_task_registry.tasks = tasks_restore


@pytest.mark.django_db()
class TestPeriodicTaskScheduling:
    @pytest.fixture(autouse=True)
    def setUp(self):
        self.mock_task_success = mock.MagicMock()
        self.mock_task_failure = mock.MagicMock(side_effect=Exception)
        with override_periodic_tasks(tasks=[
            PeriodicTaskSpec(id='task_success', schedule=timedelta(days=1), func=self.mock_task_success),
            PeriodicTaskSpec(id='task_failure', schedule=timedelta(days=1), func=self.mock_task_failure),
        ]):
            yield

    def run_tasks(self):
        async_to_sync(PeriodicTask.objects.run_all_pending_tasks)()

    def test_initial_run(self):
        self.run_tasks()
        assert PeriodicTask.objects.all().count() == 2
        assert PeriodicTask.objects.get(id='task_success').status == TaskStatus.SUCCESS
        assert PeriodicTask.objects.get(id='task_failure').status == TaskStatus.FAILED
        assert self.mock_task_success.call_count == 1
        assert self.mock_task_failure.call_count == 1

    def test_not_rerun_until_schedule(self):
        prev = PeriodicTask.objects.create(id='task_success', status=TaskStatus.SUCCESS, started=timezone.now(), completed=timezone.now())
        self.run_tasks()
        t = PeriodicTask.objects.get(id='task_success')
        assert t.status == TaskStatus.SUCCESS
        assert t.started == prev.started
        assert not self.mock_task_success.called

    def test_rerun_after_schedule(self):
        PeriodicTask.objects.create(id='task_success', status=TaskStatus.SUCCESS, started=timezone.now() - timedelta(days=2), completed=timezone.now()- timedelta(days=2))
        start_time = timezone.now()
        self.run_tasks()
        t = PeriodicTask.objects.get(id='task_success')
        assert t.status == TaskStatus.SUCCESS
        assert t.started > start_time
        assert t.completed > start_time
        assert self.mock_task_success.call_count == 1

    def test_retry(self):
        PeriodicTask.objects.create(id='task_failure', status=TaskStatus.FAILED, started=timezone.now() - timedelta(hours=2), completed=timezone.now()- timedelta(hours=2))
        start_time = timezone.now()
        self.run_tasks()
        t = PeriodicTask.objects.get(id='task_failure')
        assert t.status == TaskStatus.FAILED
        assert t.started > start_time
        assert t.completed > start_time
        assert self.mock_task_failure.call_count == 1

    def test_running_not_scheduled(self):
        running = PeriodicTask.objects.create(id='task_success', status=TaskStatus.RUNNING, started=timezone.now())
        self.run_tasks()
        t = PeriodicTask.objects.get(id='task_success')
        assert t.status == TaskStatus.RUNNING
        assert t.started == running.started
        assert t.completed == running.completed
        assert not self.mock_task_success.called

    def test_running_timeout_retry(self):
        PeriodicTask.objects.create(id='task_success', status=TaskStatus.RUNNING, started=timezone.now() - timedelta(hours=2))
        start_time = timezone.now()
        self.run_tasks()
        t = PeriodicTask.objects.get(id='task_success')
        assert t.status == TaskStatus.SUCCESS
        assert t.started > start_time
        assert t.completed > start_time
        assert self.mock_task_success.call_count == 1

    def test_db_query_performance(self):
        self.run_tasks()

        with assertNumQueries(1):
            async_to_sync(PeriodicTask.objects.run_all_pending_tasks)()


@pytest.mark.django_db()
class TestCleanupUnreferencedFiles:
    @pytest.fixture(autouse=True)
    def setUp(self):
        with override_settings(SIMPLE_HISTORY_ENABLED=False):
            yield

    def file_exists(self, file_obj):
        try:
            file_obj.file.read()
            return True
        except FileNotFoundError:
            return False

    def run_cleanup_project_files(self, num_queries, last_success=None):
        with assertNumQueries(num_queries):
            async_to_sync(cleanup_project_files)(task_info=PeriodicTaskInfo(
                spec=next(filter(lambda t: t.id == 'cleanup_unreferenced_images_and_files', periodic_task_registry.tasks)),
                model=PeriodicTask(last_success=last_success),
            ))

    def run_cleanup_user_files(self, num_queries, last_success=None):
        with assertNumQueries(num_queries):
            async_to_sync(cleanup_usernotebook_files)(task_info=PeriodicTaskInfo(
                spec=next(filter(lambda t: t.id == 'cleanup_unreferenced_images_and_files', periodic_task_registry.tasks)),
                model=PeriodicTask(last_success=last_success),
            ))

    def run_cleanup_template_files(self, num_queries, last_success=None):
        with assertNumQueries(num_queries):
            async_to_sync(cleanup_template_files)(task_info=PeriodicTaskInfo(
                spec=next(filter(lambda t: t.id == 'cleanup_unreferenced_images_and_files', periodic_task_registry.tasks)),
                model=PeriodicTask(last_success=last_success),
            ))

    def test_unreferenced_files_removed(self):
        with mock_time(before=timedelta(days=10)):
            project = create_project(
                images_kwargs=[{'name': 'image.png'}],
                files_kwargs=[{'name': 'file.pdf'}],
            )
            project_image = project.images.first()
            project_file = project.files.first()
            user = create_user(
                images_kwargs=[{'name': 'image.png'}],
                files_kwargs=[{'name': 'file.pdf'}],
            )
            user_image = user.images.first()
            user_file = user.files.first()
            template = create_template(
                images_kwargs=[{'name': 'image.png'}],
            )
            template_image = template.images.first()
        self.run_cleanup_project_files(num_queries=1 + 5 + 2 * 2 + 2 * 1)
        self.run_cleanup_user_files(num_queries=1 + 3 + 2 * 2 + 2 * 1)
        self.run_cleanup_template_files(num_queries=1 + 2 + 1 * 2 + 1 * 1)
        # Deleted from DB
        assert project.images.count() == 0
        assert project.files.count() == 0
        assert user.images.count() == 0
        assert user.files.count() == 0
        assert template.images.count() == 0
        # Deleted from FS
        assert not self.file_exists(project_image)
        assert not self.file_exists(project_file)
        assert not self.file_exists(user_image)
        assert not self.file_exists(user_file)
        assert not self.file_exists(template_image)

    def test_recently_created_unreferenced_files_not_removed(self):
        project = create_project(
            images_kwargs=[{'name': 'image.png'}],
            files_kwargs=[{'name': 'file.pdf'}],
        )
        user = create_user(
            images_kwargs=[{'name': 'image.png'}],
        )
        template = create_template(
            images_kwargs=[{'name': 'image.png'}],
        )
        self.run_cleanup_project_files(num_queries=1)
        self.run_cleanup_user_files(num_queries=1)
        self.run_cleanup_template_files(num_queries=1)
        # DB objects exist
        assert project.images.count() == 1
        assert project.files.count() == 1
        assert user.images.count() == 1
        assert template.images.count() == 1
        # Files exist
        assert self.file_exists(project.images.first())
        assert self.file_exists(project.files.first())
        assert self.file_exists(user.images.first())
        assert self.file_exists(template.images.first())

    def test_referenced_files_in_section_not_removed(self):
        with mock_time(before=timedelta(days=10)):
            project = create_project(
                report_data={'field_markdown': '![](/images/name/image.png)\n[](/files/name/file.pdf)'},
                images_kwargs=[{'name': 'image.png'}],
                files_kwargs=[{'name': 'file.pdf'}],
            )
        self.run_cleanup_project_files(num_queries=1 + 5)
        assert project.images.count() == 1
        assert project.files.count() == 1

    def test_referenced_files_in_finding_not_removed(self):
        with mock_time(before=timedelta(days=10)):
            project = create_project(
                findings_kwargs=[{'data': {'description': '![](/images/name/image.png)\n[](/files/name/file.pdf)'}}],
                images_kwargs=[{'name': 'image.png'}],
                files_kwargs=[{'name': 'file.pdf'}],
            )
        self.run_cleanup_project_files(num_queries=1 + 5)
        assert project.images.count() == 1
        assert project.files.count() == 1

    def test_referenced_files_in_notes_not_removed(self):
        with mock_time(before=timedelta(days=10)):
            project = create_project(
                notes_kwargs=[{'text': '![](/images/name/image.png)\n[](/files/name/file.pdf)'}],
                images_kwargs=[{'name': 'image.png'}],
                files_kwargs=[{'name': 'file.pdf'}],
            )
        self.run_cleanup_project_files(num_queries=1 + 5)
        assert project.images.count() == 1
        assert project.files.count() == 1

    def test_referenced_files_in_user_notes_not_removed(self):
        with mock_time(before=timedelta(days=10)):
            user = create_user(
                notes_kwargs=[{'text': '![](/images/name/image.png)\n[](/files/name/file.pdf'}],
                images_kwargs=[{'name': 'image.png'}],
                files_kwargs=[{'name': 'file.pdf'}],
            )
        self.run_cleanup_user_files(num_queries=1 + 3)
        assert user.images.count() == 1
        assert user.files.count() == 1

    def test_referenced_files_in_templates_not_removed(self):
        with mock_time(before=timedelta(days=10)):
            template = create_template(
                data={'description': '![](/images/name/image.png)'},
                images_kwargs=[{'name': 'image.png'}],
            )
        self.run_cleanup_template_files(num_queries=1 + 2)
        assert template.images.count() == 1

    def test_file_referenced_by_multiple_projects(self):
        with mock_time(before=timedelta(days=10)):
            project_unreferenced = create_project(
                name='unreferenced',
                images_kwargs=[{'name': 'image.png'}],
                files_kwargs=[{'name': 'file.pdf'}],
                report_data={'field_markdown': 'not referenced'},
            )
            project_referenced = project_unreferenced.copy(name='referenced')
            update(
                obj=project_referenced.sections.filter(section_id='other').get(),
                data={'field_markdown': '![](/images/name/image.png)\n[](/files/name/file.pdf)'})
        self.run_cleanup_project_files(num_queries=1 + 5 + 2 * 2 + 2 * 1)

        # Files deleted for unreferenced project
        assert project_unreferenced.images.count() == 0
        assert project_unreferenced.files.count() == 0
        # Files not deleted for referenced project
        assert project_referenced.images.count() == 1
        assert project_referenced.files.count() == 1
        # Files still present on filesystem
        assert self.file_exists(project_referenced.images.first())
        assert self.file_exists(project_referenced.files.first())

    def test_optimized_cleanup(self):
        with mock_time(before=timedelta(days=20)):
            project_old = create_project(
                images_kwargs=[{'name': 'image.png'}],
                files_kwargs=[{'name': 'file.pdf'}],
            )
            user_old = create_user(
                images_kwargs=[{'name': 'image.png'}],
                files_kwargs=[{'name': 'file.pdf'}],
            )
            template_old = create_template(
                images_kwargs=[{'name': 'image.png'}],
            )
            project_new = create_project(
                images_kwargs=[{'name': 'image.png'}],
                files_kwargs=[{'name': 'file.pdf'}],
            )
            user_new = create_user(
                images_kwargs=[{'name': 'image.png'}],
            )
            template_new = create_template(
                images_kwargs=[{'name': 'image.png'}],
            )
        with mock_time(before=timedelta(days=10)):
            project_new.save()
            user_new.notes.first().save()
            template_new.save()
        last_task_run = timezone.now() - timedelta(days=15)
        self.run_cleanup_project_files(num_queries=1 + 5 + 2 * 2 + 2 * 1, last_success=last_task_run)
        self.run_cleanup_user_files(num_queries=1 + 3 + 2 * 2 + 2 * 1, last_success=last_task_run)
        self.run_cleanup_template_files(num_queries=1 + 2 + 1 * 2 + 1 * 1, last_success=last_task_run)

        # Old project should be ignored because it was already cleaned in the last run
        assert project_old.images.count() == 1
        assert project_old.files.count() == 1
        assert user_old.images.count() == 1
        assert user_old.files.count() == 1
        assert template_old.images.count() == 1
        # New project should be cleaned because it was modified after the last run
        assert project_new.images.count() == 0
        assert project_new.files.count() == 0
        assert user_new.images.count() == 0
        assert user_new.files.count() == 0
        assert template_new.images.count() == 0


@pytest.mark.django_db()
class TestResetStaleArchiveRestore:
    def test_reset_stale(self):
        with mock_time(before=timedelta(days=10)):
            archive = create_archived_project(project=create_project(members=[create_user(public_key=True) for _ in range(2)]))
            keypart = update(
                obj=archive.key_parts.first(),
                decrypted_at=timezone.now(),
                key_part={'key_id': 'shamir-key-id', 'key': 'dummy-key'})

        async_to_sync(reset_stale_archive_restores)(None)

        keypart.refresh_from_db()
        assert not keypart.is_decrypted
        assert keypart.decrypted_at is None
        assert keypart.key_part is None

    def test_reset_not_stale(self):
        with mock_time(before=timedelta(days=10)):
            archive = create_archived_project(project=create_project(members=[create_user(public_key=True) for _ in range(3)]))
            keypart1 = update(
                obj=archive.key_parts.first(),
                decrypted_at=timezone.now(),
                key_part={'key_id': 'shamir-key-id', 'key': 'dummy-key'})

        keypart2 = update(
            obj=archive.key_parts.exclude(pk=keypart1.pk).first(),
            decrypted_at=timezone.now(),
            key_part={'key_id': 'shamir-key-id-2', 'key': 'dummy-key2'})

        async_to_sync(reset_stale_archive_restores)(None)

        keypart1.refresh_from_db()
        assert keypart1.is_decrypted
        assert keypart1.decrypted_at is not None
        assert keypart1.key_part is not None
        keypart2.refresh_from_db()
        assert keypart2.is_decrypted
        assert keypart2.decrypted_at is not None
        assert keypart2.key_part is not None

    def test_reset_one_but_not_other(self):
        with mock_time(before=timedelta(days=10)):
            keypart1 = update(
                obj=create_archived_project(project=create_project(members=[create_user(public_key=True) for _ in range(2)])).key_parts.first(),
                decrypted_at=timezone.now(),
                key_part={'key_id': 'shamir-key-id', 'key': 'dummy-key'})

        keypart2 = update(
            obj=create_archived_project(project=create_project(members=[create_user(public_key=True) for _ in range(2)])).key_parts.first(),
            decrypted_at=timezone.now(),
            key_part={'key_id': 'shamir-key-id', 'key': 'dummy-key'})

        async_to_sync(reset_stale_archive_restores)(None)

        keypart1.refresh_from_db()
        assert not keypart1.is_decrypted
        keypart2.refresh_from_db()
        assert keypart2.is_decrypted


@pytest.mark.django_db()
class TestAutoProjectArchiving:
    @pytest.fixture(autouse=True)
    def setUp(self):
        self.user = create_user(public_key=True)
        self.project = create_project(readonly=True, members=[self.user])

        with override_configuration(
            AUTOMATICALLY_ARCHIVE_PROJECTS_AFTER=30,
            ARCHIVING_THRESHOLD=1,
        ):
            yield

    def test_archived(self):
        create_user(public_key=True, is_global_archiver=True)
        project_active = create_project(readonly=False, members=[self.user])

        with mock_time(after=timedelta(days=40)):
            async_to_sync(automatically_archive_projects)(None)
            assert ArchivedProject.objects.filter(name=self.project.name).exists()
            assert not PentestProject.objects.filter(id=self.project.id).exists()
            assert PentestProject.objects.filter(id=project_active.id).exists()

    @override_configuration(AUTOMATICALLY_ARCHIVE_PROJECTS_AFTER=None)
    def test_auto_archiving_disabled(self):
        with mock_time(after=timedelta(days=60)):
            async_to_sync(automatically_archive_projects)(None)
            assert PentestProject.objects.filter(id=self.project.id).exists()

    def test_project_below_auto_archive_time(self):
        with mock_time(after=timedelta(days=10)):
            async_to_sync(automatically_archive_projects)(None)
            assert PentestProject.objects.filter(id=self.project.id).exists()

    def test_counter_reset_on_unfinished(self):
        with mock_time(after=timedelta(days=20)):
            update(self.project, readonly=False)
        with mock_time(after=timedelta(days=21)):
            update(self.project, readonly=True)
        with mock_time(after=timedelta(days=40)):
            async_to_sync(automatically_archive_projects)(None)
            assert PentestProject.objects.filter(id=self.project.id).exists()
        with mock_time(after=timedelta(days=60)):
            async_to_sync(automatically_archive_projects)(None)
            assert ArchivedProject.objects.filter(name=self.project.name).exists()
            assert not PentestProject.objects.filter(id=self.project.id).exists()


@pytest.mark.django_db()
class TestAutoArchiveDeletion:
    @pytest.fixture(autouse=True)
    def setUp(self):
        self.archive = create_archived_project()

        with override_configuration(AUTOMATICALLY_DELETE_ARCHIVED_PROJECTS_AFTER=30):
            yield

    def test_delete(self):
        with mock_time(after=timedelta(days=60)):
            async_to_sync(automatically_delete_archived_projects)(None)
            assert not ArchivedProject.objects.filter(id=self.archive.id).exists()

    def test_archive_blow_time(self):
        with mock_time(after=timedelta(days=20)):
            async_to_sync(automatically_delete_archived_projects)(None)
            assert ArchivedProject.objects.filter(id=self.archive.id).exists()

    @override_configuration(AUTOMATICALLY_DELETE_ARCHIVED_PROJECTS_AFTER=None)
    def test_auto_archive_disabled(self):
        with mock_time(after=timedelta(days=60)):
            async_to_sync(automatically_delete_archived_projects)(None)
            assert ArchivedProject.objects.filter(id=self.archive.id).exists()


@pytest.mark.django_db()
class TestCleanupCollabEvents:
    @pytest.fixture(autouse=True)
    def setUp(self):
        self.collab_event = CollabEvent.objects.create(
            related_id=uuid4(),
            type=CollabEventType.UPDATE_TEXT,
            path='notes',
            version=1,
            data={'updates': []},
        )

    def test_delete(self):
        with mock_time(after=timedelta(days=1)):
            async_to_sync(cleanup_collab_events)(None)
            assert not CollabEvent.objects.filter(id=self.collab_event.id).exists()

    def test_not_deleted_too_new(self):
        async_to_sync(cleanup_collab_events)(None)
        assert CollabEvent.objects.filter(id=self.collab_event.id).exists()
