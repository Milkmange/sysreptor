import os

from django.core.management import BaseCommand, CommandError

from sysreptor.users.models import PentestUser


class Command(BaseCommand):
    help = 'Create a superuser, or update the password for an existing superuser.'

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            '--username', dest='username', default=None,
        )
        parser.add_argument(
            '--password', dest='password', default=None,
            help='Specifies the password for the user.',
        )
        parser.add_argument(
            '--superuser', dest='is_superuser', action='store_true', default=False,
        )
        parser.add_argument(
            '--system', dest='is_system_user', action='store_true', default=False,
        )
        parser.add_argument(
            '--initial-saas-user', dest='is_initial_saas_user', action='store_true', default=False,
            help='Only for saas operator',
        )

    def handle(self, username, password, is_superuser, is_system_user, is_initial_saas_user, *args, **kwargs):
        username = username or os.environ.get('DJANGO_SUPERUSER_USERNAME')
        password = password or os.environ.get('DJANGO_SUPERUSER_PASSWORD')

        if not password or not username:
            raise CommandError("username and password (DJANGO_SUPERUSER_PASSWORD) must be set")
        if len(password) < 15:
            raise CommandError("password must be at least 15 characters")

        user = PentestUser.objects.filter(username=username).first()

        if is_initial_saas_user:
            if user:
                raise CommandError("User already exists")
            if PentestUser.objects.filter(is_system_user=False).count() > 0:
                raise CommandError("System already contains users, abort")

        if not user:
            user = PentestUser(username=username)

        user.set_password(password)
        if is_superuser:
            user.is_superuser = True
            user.is_staff = True
        if is_system_user:
            user.is_system_user = True
        if is_initial_saas_user:
            user.must_change_password = True
        user.save()

        self.stdout.write("User created or updated")
