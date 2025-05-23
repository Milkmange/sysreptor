import datetime
from uuid import UUID

from django.db.models.query import Prefetch, prefetch_related_objects
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from sysreptor.utils.fielddefinition.types import (
    BaseField,
    CweField,
    FieldDataType,
    FieldDefinition,
    ObjectField,
)
from sysreptor.utils.fielddefinition.validators import (
    BooleanValidatorWrapper,
    JsonSchemaValidator,
    JsonStringValidator,
    RegexPatternValidator,
)


@extend_schema_field(OpenApiTypes.OBJECT)
class DynamicObjectSerializer(serializers.Serializer):
    def __init__(self, *args, **kwargs):
        self._declared_fields = kwargs.pop('fields', {})
        super().__init__(*args, **kwargs)


class DateFieldSerializer(serializers.DateField):
    def to_internal_value(self, value):
        date = super().to_internal_value(value)
        if isinstance(date, datetime.date):
            return date.isoformat()
        else:
            return date


class CweFieldSerializer(serializers.CharField):
    def to_internal_value(self, data):
        out = super().to_internal_value(data)
        if not CweField.is_valid_cwe(out):
            raise serializers.ValidationError('Not a valid CWE')
        return out


class UserField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        from sysreptor.users.models import PentestUser
        return PentestUser.objects.all()

    def to_internal_value(self, data):
        from sysreptor.pentests.models import ProjectMemberInfo
        from sysreptor.users.models import PentestUser

        if isinstance(data, str|UUID) and (project := self.context.get('project')):
            if not getattr(project, '_prefetched_objects_cache', {}).get('members'):
                # Prefetch members to avoid N+1 queries
                prefetch_related_objects([project], Prefetch('members', ProjectMemberInfo.objects.select_related('user')))

            if member := next(filter(lambda u: str(data) == str(u.user.id), project.members.all()), None):
                return str(member.user.id)
            elif imported_user := next(filter(lambda u: data == u.get('id'), project.imported_members), None):
                return imported_user.get('id')

        user = super().to_internal_value(data)
        return str(user.id) if isinstance(user, PentestUser) else user

    def to_representation(self, value):
        if isinstance(value, str|UUID):
            return value
        return super().to_representation(value)


def serializer_from_definition(definition: FieldDefinition|ObjectField, validate_values=False, **kwargs):
    return DynamicObjectSerializer(
        fields={f.id: serializer_from_field(f, validate_values=validate_values) for f in definition.fields},
        **kwargs)


def serializer_from_field(definition: BaseField, validate_values=False, **kwargs):
    field_kwargs = kwargs | {
        'label': definition.label or definition.id,
        'required': False,
    }
    value_field_kwargs = field_kwargs | {
        'allow_null': True,
    }
    validators = []
    allow_blank = True

    if validate_values:
        field_kwargs |= {'validators': validators}
        value_field_kwargs |= {'validators': validators}

        if getattr(definition, 'required', False) and not field_kwargs.get('read_only'):
            field_kwargs |= {'required': True}
            value_field_kwargs |= {'required': True, 'allow_null': False}
            allow_blank = False
    if validate_values and (validator_fn := definition.extra_info.get('validate')):
        validators.append(BooleanValidatorWrapper(validator_fn))

    field_type = definition.type
    if field_type in [FieldDataType.STRING, FieldDataType.MARKDOWN, FieldDataType.CVSS, FieldDataType.COMBOBOX]:
        if validate_values and field_type == FieldDataType.STRING and definition.pattern:
            validators.append(RegexPatternValidator(definition.pattern))
        return serializers.CharField(trim_whitespace=False, allow_blank=allow_blank, **value_field_kwargs)
    elif field_type == FieldDataType.DATE:
        return DateFieldSerializer(**value_field_kwargs)
    elif field_type == FieldDataType.NUMBER:
        if validate_values:
            value_field_kwargs |= {
                'min_value': definition.minimum,
                'max_value': definition.maximum,
            }
        return serializers.FloatField(**value_field_kwargs)
    elif field_type == FieldDataType.BOOLEAN:
        return serializers.BooleanField(**value_field_kwargs)
    elif field_type == FieldDataType.ENUM:
        return serializers.ChoiceField(choices=[c.value for c in definition.choices], **value_field_kwargs)
    elif field_type == FieldDataType.CWE:
        return CweFieldSerializer(**value_field_kwargs)
    elif field_type == FieldDataType.USER:
        return UserField(**value_field_kwargs)
    elif field_type == FieldDataType.JSON:
        if validate_values:
            validators.append(JsonStringValidator())
            if definition.schema:
                validators.append(JsonSchemaValidator(schema=definition.schema or {}))
        return serializers.CharField(allow_blank=allow_blank, **value_field_kwargs)
    elif field_type == FieldDataType.OBJECT:
        return serializer_from_definition(definition, validate_values=validate_values, **field_kwargs)
    elif field_type == FieldDataType.LIST:
        return serializers.ListField(child=serializer_from_field(definition.items, validate_values=validate_values), allow_empty=allow_blank, **field_kwargs)
    else:
        raise ValueError(f'Encountered unsupported type in field definition: "{field_type}"')
