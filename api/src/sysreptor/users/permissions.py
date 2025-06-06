from datetime import datetime

from django.conf import settings
from django.utils import timezone
from rest_framework import authentication, exceptions, permissions

from sysreptor.users.auth import forbidden_with_apitoken_auth
from sysreptor.users.models import PentestUser
from sysreptor.utils import license
from sysreptor.utils.configuration import configuration


def check_sensitive_operation_timeout(request):
    """
    Check if the current session was fully authenticated (password + MFA) before a short period of time (settings.SENSITIVE_OPERATION_REAUTHENTICATION_TIMEOUT).
    """
    try:
        reauth_time = datetime.fromisoformat(request.session.get('authentication_info', {}).get('reauth_time'))
        if reauth_time + settings.SENSITIVE_OPERATION_REAUTHENTICATION_TIMEOUT >= timezone.now():
            return True
    except (ValueError, TypeError):
        pass
    raise exceptions.PermissionDenied(detail='Autentication timeout for sensitive operation. Log in again.', code='reauth-required')


class UserViewSetPermissions(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        if view.action == 'destroy':
            return request.user.is_admin or request.user.is_user_manager
        elif view.action == 'self':
            # Allow updating your own user
            return True
        elif view.action == 'change_password' and (configuration.LOCAL_USER_AUTH_ENABLED or not license.is_professional()):
            forbidden_with_apitoken_auth(request)
            return check_sensitive_operation_timeout(request)
        elif view.action == 'enable_admin_permissions':
            forbidden_with_apitoken_auth(request)
            return license.ProfessionalLicenseRequired().has_permission(request, view) and request.user.is_superuser and check_sensitive_operation_timeout(request)
        elif view.action == 'disable_admin_permissions':
            forbidden_with_apitoken_auth(request)
            return license.ProfessionalLicenseRequired().has_permission(request, view) and request.user.is_admin
        return request.user.is_user_manager or request.user.is_admin

    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        if obj.is_system_user and obj != request.user:
            return False
        if view.action in ['reset_password', 'destroy']:
            if obj.is_superuser and not request.user.is_admin:
                # Prevent user_managers from resetting superuser password
                # This would be a privilege escalation
                return False
        if view.action == 'destroy' and request.user == obj:
            # Prevent deleting yourself
            return False
        return True


class MFAMethodViewSetPermissons(permissions.BasePermission):
    def has_permission(self, request, view):
        forbidden_with_apitoken_auth(request)
        if not configuration.LOCAL_USER_AUTH_ENABLED and license.is_professional():
            return False

        user = view.get_user()
        if user == request.user:
            check_sensitive_operation_timeout(request)
            return True

        if not request.user.is_admin and not request.user.is_user_manager:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        if view.action not in ['list', 'retrieve', 'destroy']:
            return False
        if request.user.is_user_manager and user.is_superuser:
            return False
        return True


class AuthIdentityViewSetPermissions(permissions.BasePermission):
    def has_permission(self, request, view):
        forbidden_with_apitoken_auth(request)
        user = view.get_user()
        if request.method in permissions.SAFE_METHODS:
            return request.user.is_admin or request.user.is_user_manager or user == request.user
        else:
            if user.is_system_user:
                return False
            return request.user.is_admin or (request.user.is_user_manager and not user.is_superuser)


class APITokenViewSetPermissions(permissions.BasePermission):
    def has_permission(self, request, view):
        forbidden_with_apitoken_auth(request)
        user = view.get_user()
        if view.kwargs.get('pentestuser_pk') == 'self':
            return check_sensitive_operation_timeout(request)
        if not request.user.is_admin and not request.user.is_user_manager:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        if view.action not in ['list', 'retrieve', 'destroy']:
            return False
        if request.user.is_user_manager and user.is_superuser:
            return False
        return True


class MFALoginInProgressAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        if user_id := request.session.get('login_state', {}).get('user_id'):
            return PentestUser.objects.get(id=user_id), None


class RemoteUserAuthPermissions(permissions.BasePermission):
    def has_permission(self, request, view):
        return configuration.REMOTE_USER_AUTH_ENABLED and license.ProfessionalLicenseRequired().has_permission(request, view)


class LocalUserAuthPermissions(permissions.BasePermission):
    def has_permission(self, request, view):
        return configuration.LOCAL_USER_AUTH_ENABLED or not license.is_professional()


class ForgotPasswordPermissions(LocalUserAuthPermissions):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and \
              license.ProfessionalLicenseRequired().has_permission(request, view) and \
              configuration.FORGOT_PASSWORD_ENABLED and configuration.LOCAL_USER_AUTH_ENABLED and \
              settings.EMAIL_HOST


class UserNotebookPermissions(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.user == view.get_user():
            return True
        if request.user.is_admin and (request.method in permissions.SAFE_METHODS or view.action in ['export_pdf']):
            return True
        return False
