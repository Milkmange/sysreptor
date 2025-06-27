import copy
import itertools
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from sysreptor.pentests.collab.text_transformations import SelectionRange
from sysreptor.pentests.fielddefinition.predefined_fields import (
    FINDING_FIELDS_CORE,
    FINDING_FIELDS_PREDEFINED,
    REPORT_FIELDS_CORE,
    finding_fields_default,
    report_sections_default,
)
from sysreptor.pentests.fielddefinition.sort import group_findings, sort_findings
from sysreptor.pentests.models import FindingTemplate, FindingTemplateTranslation, Language
from sysreptor.pentests.models.project import Comment
from sysreptor.pentests.rendering.entry import format_template_field_object
from sysreptor.tests.mock import (
    api_client,
    create_comment,
    create_finding,
    create_project,
    create_project_type,
    create_template,
    create_user,
    update,
)
from sysreptor.tests.utils import assertKeysEqual
from sysreptor.utils.fielddefinition.mixins import CustomFieldsMixin
from sysreptor.utils.fielddefinition.serializers import serializer_from_definition
from sysreptor.utils.fielddefinition.types import (
    FieldDataType,
    FieldDefinition,
    ListField,
    StringField,
    parse_field_definition,
    parse_field_definition_legacy,
    serialize_field_definition,
    serialize_field_definition_legacy,
)
from sysreptor.utils.fielddefinition.utils import (
    HandleUndefinedFieldsOptions,
    check_definitions_compatible,
    ensure_defined_structure,
)
from sysreptor.utils.fielddefinition.validators import FieldDefinitionValidator, FieldValuesValidator


@pytest.mark.parametrize(('valid', 'definition'), [
    (True, []),
    (False, [{'id': 'f'}]),
    (True, [{'id': 'f', 'type': 'string'}]),
    # Test field id
    (True, [{'id': 'field1', 'type': 'string', 'label': 'Field 1', 'default': None}]),
    (True, [{'id': 'fieldNumber_one', 'type': 'string', 'label': 'Field 1', 'default': None}]),
    (False, [{'id': 'field 1', 'type': 'string', 'label': 'Field 1', 'default': None}]),
    (False, [{'id': 'field.one', 'type': 'string', 'label': 'Field 1', 'default': None}]),
    (False, [{'id': '1st_field', 'type': 'string', 'label': 'Field 1', 'default': None}]),
    # Test duplicate IDs
    (False, [{'id': 'f', 'type': 'string', 'label': 'Field 1', 'default': None}, {'id': 'f', 'type': 'string', 'label': 'Field 1', 'default': None}]),
    (False, [{'id': 'f', 'type': 'object', 'properties': [{'id': 'f', 'type': 'string', 'label': 'Field 1', 'default': None}, {'id': 'f', 'type': 'string', 'label': 'Field 1', 'default': None}]}]),
    (False, [{'id': 'f', 'type': 'list', 'items': {'id': 'f', 'type': 'object', 'properties': [{'id': 'f', 'type': 'string', 'label': 'Field 1', 'default': None}, {'id': 'f', 'type': 'string', 'label': 'Field 1', 'default': None}]}}]),
    # Test data types
    (True, [
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
        {'id': 'field_object', 'type': 'object', 'label': 'Nested Object', 'properties': [{'id': 'nested1', 'type': 'string', 'label': 'Nested Field'}]},
        {'id': 'field_list', 'type': 'list', 'label': 'List Field', 'items': {'type': 'string'}},
        {'id': 'field_list_objects', 'type': 'list', 'label': 'List of nested objects', 'items': {'type': 'object', 'properties': [{'id': 'nested1', 'type': 'string', 'label': 'Nested object field', 'default': None}]}},
    ]),
    (False, [{'id': 'f', 'type': 'unknown', 'label': 'Unknown'}]),
    (False, [{'id': 'f', 'type': 'date', 'label': 'Date', 'default': 'not a date'}]),
    (False, [{'id': 'f', 'type': 'number', 'label': 'Number', 'default': 'not an int'}]),
    (False, [{'id': 'f', 'type': 'enum', 'label': 'Enum Filed'}]),
    (False, [{'id': 'f', 'type': 'enum', 'label': 'Enum Field', 'choices': []}]),
    (False, [{'id': 'f', 'type': 'enum', 'label': 'Enum Field', 'choices': [{'value': 'v1'}]}]),
    (False, [{'id': 'f', 'type': 'enum', 'label': 'Enum Field', 'choices': [{'value': None}]}]),
    (False, [{'id': 'f', 'type': 'enum', 'label': 'Enum Field', 'choices': [{'label': 'Name only'}]}]),
    (False, [{'id': 'f', 'type': 'cwe', 'label': 'CWE Field', 'default': 'not a CWE'}]),
    (False, [{'id': 'f', 'type': 'combobox'}]),
    (False, [{'id': 'f', 'type': 'combobox', 'suggestions': [None]}]),
    (False, [{'id': 'f', 'type': 'object', 'label': 'Object Field'}]),
    (False, [{'id': 'f', 'type': 'object', 'label': 'Object Field', 'properties': [{'id': 'adsf'}]}]),
    (False, [{'id': 'f', 'type': 'list', 'label': 'List Field'}]),
    (False, [{'id': 'f', 'type': 'list', 'label': 'List Field', 'items': {}}]),
    (False, [{'id': 'field_list', 'type': 'list', 'label': 'List Field', 'items': {'type': 'string'}, 'default': 'not a list'}]),
])
def test_definition_formats(valid, definition):
    res_valid = True
    try:
        FieldDefinitionValidator()(definition)
    except ValidationError:
        res_valid = False
    assert res_valid == valid


@pytest.mark.parametrize(('definition_old', 'definition_new'), [
    (
        {'f': {'type': 'string', 'label': 'String Field', 'origin': 'custom', 'help_text': None, 'default': None, 'required': True, 'spellcheck': True, 'pattern': None}},
        [{'id': 'f', 'type': 'string', 'label': 'String Field', 'origin': 'custom', 'help_text': None, 'default': None, 'required': True, 'spellcheck': True, 'pattern': None}],
    ),
    (
        {'f': {'type': 'list', 'label': 'List Field', 'origin': 'custom', 'help_text': None, 'required': False, 'items': {'type': 'string', 'label': 'Item', 'origin': 'custom', 'help_text': None, 'default': None, 'required': True, 'spellcheck': True, 'pattern': None}}},
        [{'id': 'f', 'type': 'list', 'label': 'List Field', 'origin': 'custom', 'help_text': None, 'default': None, 'required': False, 'items': {'id': '', 'type': 'string', 'label': 'Item', 'origin': 'custom', 'help_text': None, 'default': None, 'required': True, 'spellcheck': True, 'pattern': None}}],
    ),
    (
        {'f': {'type': 'object', 'label': 'Object Field', 'origin': 'custom', 'help_text': None, 'properties': {'nested': {'type': 'string', 'label': 'String Field', 'origin': 'custom', 'help_text': None, 'default': None, 'required': True, 'spellcheck': True, 'pattern': None}}}},
        [{'id': 'f', 'type': 'object', 'label': 'Object Field', 'origin': 'custom', 'help_text': None, 'properties': [{'id': 'nested', 'type': 'string', 'label': 'String Field', 'origin': 'custom', 'help_text': None, 'default': None, 'required': True, 'spellcheck': True, 'pattern': None}]}],
    ),
    (
        {'f': {'type': 'list', 'label': 'List Field', 'origin': 'custom', 'help_text': None, 'required': False, 'items': {'type': 'object', 'label': 'Object Field', 'origin': 'custom', 'help_text': None, 'properties': {'nested': {'type': 'string', 'label': 'String Field', 'origin': 'custom', 'help_text': None, 'default': None, 'required': True, 'spellcheck': True, 'pattern': None}}}}},
        [{'id': 'f', 'type': 'list', 'label': 'List Field', 'origin': 'custom', 'help_text': None, 'default': None, 'required': False, 'items': {'id': '', 'type': 'object', 'label': 'Object Field', 'origin': 'custom', 'help_text': None, 'properties': [{'id': 'nested', 'type': 'string', 'label': 'String Field', 'origin': 'custom', 'help_text': None, 'default': None, 'required': True, 'spellcheck': True, 'pattern': None}]}}],
    ),
])
def test_legacy_definition_format(definition_old, definition_new):
    FieldDefinitionValidator()(definition_new)
    assert serialize_field_definition(parse_field_definition_legacy(definition_old)) == definition_new
    assert serialize_field_definition_legacy(parse_field_definition(definition_new)) == definition_old


@pytest.mark.parametrize(('valid', 'definition', 'value'), [
    (True, [
            {'id': 'field_string', 'type': 'string', 'label': 'String Field', 'default': 'test'},
            {'id': 'field_string2', 'type': 'string', 'label': 'String Field', 'default': None},
            {'id': 'field_markdown', 'type': 'markdown', 'label': 'Markdown Field', 'default': '# test\nmarkdown'},
            {'id': 'field_cvss', 'type': 'cvss', 'label': 'CVSS Field', 'default': 'n/a'},
            {'id': 'field_cwe', 'type': 'cwe', 'label': 'CWE Field', 'default': None},
            {'id': 'field_json', 'type': 'json', 'label': 'JSON Field', 'default': None},
            {'id': 'field_date', 'type': 'date', 'label': 'Date Field', 'default': '2022-01-01'},
            {'id': 'field_int', 'type': 'number', 'label': 'Number Field', 'default': 10},
            {'id': 'field_bool', 'type': 'boolean', 'label': 'Boolean Field', 'default': False},
            {'id': 'field_enum', 'type': 'enum', 'label': 'Enum Field', 'choices': [{'value': 'enum1', 'label': 'Enum Value 1'}, {'value': 'enum2', 'label': 'Enum Value 2'}], 'default': 'enum2'},
            {'id': 'field_combobox', 'type': 'combobox', 'lable': 'Combobox Field', 'suggestions': ['a', 'b']},
            {'id': 'field_object', 'type': 'object', 'label': 'Nested Object', 'properties': [{'id': 'nested1', 'type': 'string', 'label': 'Nested Field'}]},
            {'id': 'field_list', 'type': 'list', 'label': 'List Field', 'items': {'type': 'string'}},
            {'id': 'field_list_objects', 'type': 'list', 'label': 'List of nested objects', 'items': {'type': 'object', 'properties': [{'id': 'nested1', 'type': 'string', 'label': 'Nested object field', 'default': None}]}},
        ], {
            'field_string': 'This is a string',
            'field_string2': None,
            'field_markdown': 'Some **markdown**\n* String\n*List',
            'field_cvss': 'CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:C/C:H/I:H/A:H',
            'field_cwe': 'CWE-89',
            'field_json': json.dumps({'key': 'value', 'custom': ['prop']}),
            'field_date': '2022-01-01',
            'field_int': 17,
            'field_bool': True,
            'field_enum': 'enum2',
            'field_combobox': 'value2',
            'field_object': {'nested1': 'val'},
            'field_list': ['test'],
            'field_list_objects': [{'nested1': 'test'}, {'nested1': 'values'}],
            'field_additional': 'test',
        }),
    (False, [{'id': 'f', 'type': 'string'}], {'f': {}}),
    (False, [{'id': 'f', 'type': 'string'}], {}),
    (False, [{'id': 'f', 'type': 'cwe'}], {'f': 'not a CWE'}),
    (False, [{'id': 'f', 'type': 'cwe'}], {'f': 'CWE-99999999'}),
    (False, [{'id': 'f', 'type': 'list', 'items': {'type': 'object', 'properties': [{'id': 'f', 'type': 'string'}]}}], {'f': [{'f': 'v'}, {'f': 1}]}),
    (True, [{'id': 'f', 'type': 'list', 'items': {'type': 'object', 'properties': [{'id': 'f', 'type': 'string'}]}}], {'f': [{'f': 'v'}, {'f': None}]}),
    (True, [{'id': 'f', 'type': 'list', 'items': {'type': 'string'}}], {'f': []}),
    (False, [{'id': 'f', 'type': 'list', 'items': {'type': 'string'}}], {'f': None}),
    (True, [{'id': 'f', 'type': 'combobox', 'suggestions': ['a', 'b']}], {'f': 'other'}),
    # (False, {'f': {'type': 'user'}}, {'f': str(uuid4())}),
])
def test_field_values(valid, definition, value):
    res_valid = True
    try:
        FieldValuesValidator(parse_field_definition(definition))(value)
    except (ValidationError, ValueError):
        res_valid = False
    assert res_valid == valid


@pytest.mark.django_db()
def test_user_field_value():
    user = create_user()
    definition = parse_field_definition([{'id': 'field_user', 'type': 'user', 'label': 'User Field'}])
    FieldValuesValidator(definition)({'field_user': str(user.id)})


@pytest.mark.django_db()
def test_api_serializer():
    user = create_user()
    project = create_project(members=[user])
    client = api_client(user)

    field_data = {
        'field_string': 'This is a string',
        'field_markdown': 'Some **markdown**\n* String\n*List',
        'field_cvss': 'CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:C/C:H/I:H/A:H',
        'field_cwe': 'CWE-89',
        'field_date': '2024-01-01',
        'field_int': 17,
        'field_bool': True,
        'field_enum': 'enum1',
        'field_combobox': 'value2',
        'field_user': str(user.id),
        'field_list': ['test'],
        'field_object': {'nested1': 'val'},
    }

    res1 = client.patch(reverse('section-detail', kwargs={'project_pk': project.id, 'id': 'other'}), data={
        'data': field_data,
    })
    assert res1.status_code == 200, res1.data

    res2 = client.patch(reverse('finding-detail', kwargs={'project_pk': project.id, 'id': project.findings.first().finding_id}), data={
        'data': field_data,
    })
    assert res2.status_code == 200, res2.data


@pytest.mark.django_db()
def test_api_serializer_user():
    user = create_user()
    user_imported = {
        'id': str(uuid4()),
        'name': 'Imported User',
    }
    project = create_project(members=[user], imported_members=[user_imported])
    client = api_client(user)

    def assert_valid_user_field_value(user_id, expected):
        res = client.patch(reverse('section-detail', kwargs={'project_pk': project.id, 'id': 'other'}), data={
            'data': {'field_user': user_id},
        })
        assert (res.status_code == 200) is expected

    assert_valid_user_field_value(str(user.id), True)  # Project member
    assert_valid_user_field_value(user_imported['id'], True)  # Imported member
    assert_valid_user_field_value(str(uuid4()), False)  # Nonexistent user


@pytest.mark.parametrize(('definition', 'value', 'expected'), [
    ([{'id': 'f', 'type': 'string', 'required': True}], {'f': None}, False),
    ([{'id': 'f', 'type': 'string', 'pattern': '^[0-9a-f]+$'}], {'f': 'abc123'}, True),
    ([{'id': 'f', 'type': 'string', 'pattern': '^[0-9a-f]+$'}], {'f': 'not hex'}, False),
    ([{'id': 'f', 'type': 'number', 'minimum': 1}], {'f': 0}, False),
    ([{'id': 'f', 'type': 'number', 'maximum': 5}], {'f': 7}, False),
    ([{'id': 'f', 'type': 'number', 'minimum': 1, 'maximum': 5}], {'f': 3}, True),
    ([{'id': 'f', 'type': 'json', 'schema': {'type': 'object', 'properties': {'prop': {'type': 'string'}}}}], {'f': '{"prop": "test"}'}, True),
    ([{'id': 'f', 'type': 'json', 'schema': {'type': 'object', 'properties': {'prop': {'type': 'string'}}}}], {'f': '["invalid", "schema"]'}, False),
    ([{'id': 'f', 'type': 'json', 'schema': {'type': 'object', 'properties': {'prop': {'type': 'string'}}}}], {'f': 'invalid JSON'}, False),
])
def test_api_serializer_validation(definition, value, expected):
    actual = serializer_from_definition(definition=parse_field_definition(definition), validate_values=True, data=value).is_valid(raise_exception=False)
    assert actual is expected


class CustomFieldsTestModel(CustomFieldsMixin):
    def __init__(self, field_definition, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._field_definition = parse_field_definition(field_definition)

    @property
    def field_definition(self):
        return self._field_definition


@pytest.mark.parametrize(('definition', 'old_value', 'new_value'), [
    ([{'id': 'a', 'type': 'string'}], {'a': 'old'}, {'a': 'new'}),
    ([{'id': 'a', 'type': 'string'}], {'a': 'text'}, {'a': None}),
    ([{'id': 'a', 'type': 'number'}], {'a': 10}, {'a': None}),
    ([{'id': 'a', 'type': 'enum', 'choices': [{'value': 'a'}]}], {'a': 'a'}, {'a': None}),
    ([{'id': 'a', 'type': 'list', 'items': {'type': 'enum', 'choices': [{'value': 'a'}]}}], {'a': ['a', 'a']}, {'a': ['a', None]}),
    ([{'id': 'a', 'type': 'list', 'items': {'type': 'string'}}], {'a': ['text']}, {'a': []}),
    ([{'id': 'a', 'type': 'object', 'properties': [{'id': 'b', 'type': 'string'}]}], {'a': {'b': 'old'}}, {'a': {'b': 'new'}}),
])
def test_update_field_values(definition, old_value, new_value):
    m = CustomFieldsTestModel(field_definition=definition, custom_fields=old_value)
    m.update_data(new_value)
    assert m.data == new_value


@pytest.mark.parametrize(('compatible', 'a', 'b'), [
    (True, [{'id': 'a', 'type': 'string'}], [{'id': 'b', 'type': 'string'}]),
    (True, [{'id': 'a', 'type': 'string'}], [{'id': 'a', 'type': 'string'}]),
    (True, [{'id': 'a', 'type': 'string', 'label': 'left', 'default': 'left', 'required': False}], [{'id': 'a', 'type': 'string', 'label': 'right', 'default': 'right', 'required': True}]),
    (True, [{'id': 'a', 'type': 'string'}], [{'id': 'a', 'type': 'string'}, {'id': 'b', 'type': 'string'}]),
    (True, [{'id': 'a', 'type': 'string'}, {'id': 'b', 'type': 'string'}], [{'id': 'a', 'type': 'string'}]),
    (True, [{'id': 'a', 'type': 'string'}, {'id': 'b', 'type': 'string'}], [{'id': 'b', 'type': 'string'}, {'id': 'a', 'type': 'string'}]),
    (False, [{'id': 'a', 'type': 'string'}], [{'id': 'a', 'type': 'list', 'items': {'type': 'string'}}]),
    (False, [{'id': 'a', 'type': 'string'}], [{'id': 'a', 'type': 'markdown'}]),
    (True, [{'id': 'a', 'type': 'list', 'items': {'type': 'string'}}], [{'id': 'a', 'type': 'list', 'items': {'type': 'string'}}]),
    (False, [{'id': 'a', 'type': 'list', 'items': {'type': 'string'}}], [{'id': 'a', 'type': 'list', 'items': {'type': 'number'}}]),
    (True, [{'id': 'a', 'type': 'object', 'properties': [{'id': 'a', 'type': 'string'}]}], [{'id': 'a', 'type': 'object', 'properties': [{'id': 'a', 'type': 'string'}]}]),
    (False, [{'id': 'a', 'type': 'object', 'properties': [{'id': 'a', 'type': 'string'}]}], [{'id': 'a', 'type': 'object', 'properties': [{'id': 'a', 'type': 'boolean'}]}]),
    (True, [{'id': 'a', 'type': 'object', 'properties': [{'id': 'a', 'type': 'string'}, {'id': 'b', 'type': 'string'}]}], [{'id': 'a', 'type': 'object', 'properties': [{'id': 'b', 'type': 'string'}, {'id': 'a', 'type': 'string'}]}]),
    (True, [{'id': 'a', 'type': 'enum', 'choices': [{'value': 'a'}]}], [{'id': 'a', 'type': 'enum', 'choices': [{'value': 'a'}]}]),
    (True, [{'id': 'a', 'type': 'enum', 'choices': [{'value': 'a'}]}], [{'id': 'a', 'type': 'enum', 'choices': [{'value': 'a'}, {'value': 'b'}]}]),
    (False, [{'id': 'a', 'type': 'enum', 'choices': [{'value': 'a'}, {'value': 'b'}]}], [{'id': 'a', 'type': 'enum', 'choices': [{'value': 'a'}]}]),
    (True, [{'id': 'a', 'type': 'combobox', 'suggestions': ['a']}], [{'id': 'a', 'type': 'combobox', 'choices': ['b']}]),
])
def test_definitions_compatible(compatible, a, b):
    assert check_definitions_compatible(parse_field_definition(a), parse_field_definition(b))[0] == compatible


@pytest.mark.parametrize(('definition', 'expected'), [
    ({'type': 'string'}, None),
    ({'type': 'string', 'default': None}, None),
    ({'type': 'string', 'default': 'default'}, 'default'),
    ({'type': 'boolean'}, None),
    ({'type': 'boolean', 'default': True}, True),
    ({'type': 'boolean', 'default': False}, False),
    ({'type': 'list', 'items': {'type': 'string'}}, []),
    ({'type': 'list', 'items': {'type': 'string'}, 'default': None}, []),
    ({'type': 'list', 'items': {'type': 'string'}, 'default': ['default', 'list']}, ['default', 'list']),
    ({'type': 'object', 'properties': [{'id': 'p', 'type': 'string'}]}, {'p': None}),
    ({'type': 'object', 'properties': [{'id': 'p', 'type': 'string', 'default': 'default'}]}, {'p': 'default'}),
    ({'type': 'list', 'items': {'type': 'object', 'properties': [{'id': 'p', 'type': 'string', 'default': 'default'}]}, 'default': [{}]}, [{'p': 'default'}]),
    ({'type': 'list', 'items': {'type': 'object', 'properties': [{'id': 'p', 'type': 'string', 'default': 'default'}]}, 'default': [{'p': 'list'}]}, [{'p': 'list'}]),
])
def test_ensure_defined_structure_fill_default(definition, expected):
    actual = ensure_defined_structure(value={'f': None}, definition=parse_field_definition([{'id': 'f'} | definition]), handle_undefined=HandleUndefinedFieldsOptions.FILL_DEFAULT)
    assert actual['f'] == expected


def get_definition(definition: list[dict], path: str|tuple[str]):
    if isinstance(path, str):
        path = path.split('.')
    for f in definition:
        if f['id'] == path[0]:
            if len(path) == 1:
                return f
            elif 'fields' in f:
                return get_definition(f['fields'], path[1:])
            elif f.get('type') == 'object':
                return get_definition(f['properties'], path[1:])
    raise ValueError('Field not found')


@pytest.mark.django_db()
class TestUpdateFieldDefinition:
    @pytest.fixture(autouse=True)
    def setUp(self) -> None:
        self.project_type = create_project_type()
        self.project = create_project(project_type=self.project_type, findings_kwargs=[{}])
        self.finding = self.project.findings.first()

        self.project_other = create_project(findings_kwargs=[{}])
        self.finding_other = self.project_other.findings.first()

    def refresh_data(self):
        self.project_type.refresh_from_db()
        self.project.refresh_from_db()
        self.finding.refresh_from_db()
        self.project_other.refresh_from_db()
        self.finding_other.refresh_from_db()

    def test_add_report_field(self):
        default_value = 'new'
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        get_definition(self.project_type.report_sections, 'other')['fields'].append({'id': 'field_new', 'type': 'string', 'label': 'New field', 'default': default_value})
        self.project_type.save()
        self.refresh_data()

        section = self.project.sections.get(section_id='other')
        assert 'field_new' in [f['id'] for f in section.section_definition['fields']]
        assert self.project_type.report_preview_data['report']['field_new'] == default_value

        # New field added to projects
        assert 'field_new' in section.data
        assert section.data['field_new'] == default_value

        assert 'field_new' not in self.project_other.data_all

    def test_add_finding_field(self):
        default_value = 'new'
        self.project_type.finding_fields += [
            {'id': 'field_new', 'type': 'string', 'label': 'New field', 'default': default_value},
        ]
        self.project_type.save()
        self.refresh_data()

        assert self.project_type.finding_fields[-1]['id'] == 'field_new'
        assert self.project_type.report_preview_data['findings'][0]['field_new'] == default_value

        # New field added to projects
        assert 'field_new' in self.finding.data
        assert self.finding.data['field_new'] == default_value

        assert 'field_new' not in self.finding_other.data

    def test_delete_report_field(self):
        old_value = self.project.data['field_string']
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        get_definition(self.project_type.report_sections, 'other')['fields'] = [f for f in get_definition(self.project_type.report_sections, 'other')['fields'] if f['id'] != 'field_string']
        self.project_type.save()
        self.refresh_data()

        assert 'field_string' not in set(map(lambda f: f['id'], itertools.chain(*map(lambda s: s['fields'], self.project_type.report_sections))))
        assert 'field_string' not in self.project_type.report_preview_data['report']

        # Field removed from project (but data is kept in DB)
        assert 'field_string' not in self.project.data
        assert 'field_string' in self.project.data_all
        assert self.project.data_all['field_string'] == old_value

        assert 'field_string' in self.project_other.data

    def test_delete_finding_field(self):
        old_value = self.finding.data['field_string']
        self.project_type.finding_fields = [f for f in self.project_type.finding_fields if f['id'] != 'field_string']
        self.project_type.save()
        self.refresh_data()

        assert 'field_string' not in [f['id'] for f in self.project_type.finding_fields]
        assert 'field_string' not in self.project_type.report_preview_data['findings'][0]

        # Field remove from project (but data is kept in DB)
        assert 'field_string' not in self.finding.data
        assert 'field_string' in self.finding.data_all
        assert self.finding.data_all['field_string'] == old_value

        assert 'field_string' in self.finding_other.data

    def test_change_type_report_field(self):
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        get_definition(self.project_type.report_sections, 'other.field_string').update(
            {'type': 'object', 'label': 'Changed type', 'properties': [{'id': 'nested', 'type': 'string', 'label': 'Nested field', 'default': 'default'}]},
        )
        self.project_type.save()
        self.refresh_data()

        assert isinstance(self.project_type.report_preview_data['report']['field_string'], dict)
        section = self.project.sections.get(section_id='other')
        assert section.data['field_string'] == {'nested': 'default'}

    def test_change_type_finding_field(self):
        self.project_type.finding_fields = copy.deepcopy(self.project_type.finding_fields)
        get_definition(self.project_type.finding_fields, 'field_string').update(
            {'type': 'object', 'label': 'Changed type', 'properties': [{'id': 'nested', 'type': 'string', 'label': 'Nested field', 'default': 'default'}]},
        )
        self.project_type.save()
        self.refresh_data()

        assert isinstance(self.project_type.report_preview_data['findings'][0]['field_string'], dict)
        assert self.finding.data['field_string'] == {'nested': 'default'}

    def test_change_default_report_field(self):
        default_val = 'changed'
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        get_definition(self.project_type.report_sections, 'other.field_string')['default'] = default_val
        self.project_type.save()
        self.refresh_data()

        assert self.project_type.report_preview_data['report']['field_string'] == default_val

        assert self.project.data['field_string'] != default_val

        project_new = create_project(project_type=self.project_type)
        assert project_new.data['field_string'] == default_val

    def test_change_default_finding_field(self):
        default_val = 'changed'
        self.project_type.finding_fields = copy.deepcopy(self.project_type.finding_fields)
        get_definition(self.project_type.finding_fields, 'field_string')['default'] = default_val
        self.project_type.save()
        self.refresh_data()

        for f in self.project_type.report_preview_data['findings']:
            assert f['field_string'] == default_val

        assert self.finding.data['field_string'] != default_val

        finding_new = create_finding(project=self.project)
        assert finding_new.data['field_string'] == default_val

    def test_restore_data_report_field(self):
        old_value = self.project.data['field_string']
        old_definition = get_definition(self.project_type.report_sections, 'other.field_string')

        # Delete field from definition
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        get_definition(self.project_type.report_sections, 'other')['fields'] = [f for f in get_definition(self.project_type.report_sections, 'other')['fields'] if f['id'] != 'field_string']
        self.project_type.save()
        self.refresh_data()
        assert 'field_string' not in self.project.data
        assert self.project.data_all['field_string'] == old_value

        # Restore field in definition
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        get_definition(self.project_type.report_sections, 'other')['fields'].append(old_definition | {'labal': 'Changed name', 'default': 'other'})
        self.project_type.save()
        self.refresh_data()
        assert self.project.data['field_string'] == old_value

    def test_restore_data_finding_field(self):
        old_value = self.finding.data['field_string']
        old_definition = get_definition(self.project_type.finding_fields, 'field_string')

        # Delete field from definition
        self.project_type.finding_fields = [f for f in self.project_type.finding_fields if f['id'] != 'field_string']
        self.project_type.save()
        self.refresh_data()
        assert 'field_string' not in self.finding.data
        assert self.finding.data_all['field_string'] == old_value

        # Restore field in definition
        self.project_type.finding_fields += [old_definition | {'labal': 'Changed name', 'default': 'other'}]
        self.project_type.save()
        self.refresh_data()
        assert self.finding.data['field_string'] == old_value

    def test_change_project_type_report_fields(self):
        old_value = self.project.data['field_string']
        project_type_new = create_project_type(report_sections=[{'id': 'other', 'label': 'Other', 'fields': serialize_field_definition(REPORT_FIELDS_CORE | FieldDefinition(fields=[
            StringField(id='field_new', default='default', label='New field'),
        ]))}])
        update(self.project, project_type=project_type_new)
        self.refresh_data()

        assert 'field_string' not in self.project.data
        assert self.project.data_all['field_string'] == old_value
        assert self.project.data['field_new'] == 'default'

    def test_change_project_type_finding_fields(self):
        old_value = self.project.data['field_string']
        project_type_new = create_project_type(finding_fields=serialize_field_definition(FINDING_FIELDS_CORE | FieldDefinition(fields=[
            StringField(id='field_new', default='default', label='New field'),
        ])))
        update(self.project, project_type=project_type_new)
        self.refresh_data()

        assert 'field_string' not in self.finding.data
        assert self.finding.data_all['field_string'], old_value
        assert self.finding.data['field_new'] == 'default'

    def test_change_default_report_field_sync_previewdata(self):
        # If preview_data == default => update to new default value
        default_val = 'default changed'
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        get_definition(self.project_type.report_sections, 'other.field_string')['default'] = default_val
        self.project_type.save()
        self.refresh_data()
        assert self.project_type.report_preview_data['report']['field_string'] == default_val

        # If preview_data != default => do not update
        preview_data_value = 'non-default value'
        self.project_type.report_preview_data['report']['field_string'] = preview_data_value
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        get_definition(self.project_type.report_sections, 'other.field_string')['default'] = 'default changed 2'
        self.project_type.save()
        assert self.project_type.report_preview_data['report']['field_string'] == preview_data_value


@pytest.mark.django_db()
class TestUpdateFieldDefinitionSyncComments:
    @pytest.fixture(autouse=True)
    def setUp(self):
        self.project_type = create_project_type()
        initial_data = ensure_defined_structure({
            'field_markdown': 'Example text',
            'field_cvss': 'n/a',
            'field_list': ['item'],
            'field_object': {'field_string': 'Example text'},
            'field_list_objects': [{'field_markdown': 'Example text'}],
        }, definition=self.project_type.finding_fields_obj, handle_undefined=HandleUndefinedFieldsOptions.FILL_DEFAULT)
        self.project = create_project(project_type=self.project_type, report_data=initial_data, findings_kwargs=[{'data': initial_data}], comments=False)
        self.finding = self.project.findings.first()
        self.section = self.project.sections.get(section_id='other')

        self.comment_paths = ['data.field_markdown', 'data.field_cvss', 'data.field_list.[0]', 'data.field_object.field_string', 'data.field_list_objects.[0].field_markdown']
        for p in self.comment_paths:
            create_comment(finding=self.finding, path=p, text_range=SelectionRange(anchor=0, head=10) if 'field_markdown' in p else None)
            create_comment(section=self.section, path=p, text_range=SelectionRange(anchor=0, head=10) if 'field_markdown' in p else None)

    def test_update_projecttype_delete_fields(self):
        update(self.project_type, finding_fields=finding_fields_default(), report_sections=report_sections_default())
        assert Comment.objects.filter_project(self.project).count() == 0

    def test_change_projecttype_delete_fields(self):
        pt2 = create_project_type()
        update(pt2, finding_fields=finding_fields_default(), report_sections=report_sections_default())
        update(self.project, project_type=pt2)

        assert Comment.objects.filter_project(self.project).count() == 0

    def test_fields_deleted(self):
        self.project_type.finding_fields = copy.deepcopy(self.project_type.finding_fields)
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        for d in [self.project_type.finding_fields, get_definition(self.project_type.report_sections, 'other')['fields']]:
            get_definition(d, 'field_markdown')['id'] = 'field_markdown_renamed'
            get_definition(d, 'field_list')['type'] = 'string'
            d.remove(get_definition(d, 'field_cvss'))
            d_props = get_definition(d, 'field_object')['properties']
            d_props.remove(get_definition(d_props, 'field_string'))
            d_item_props = get_definition(d, 'field_list_objects')['items']['properties']
            d_item_props.remove(get_definition(d_item_props, 'field_markdown'))
        self.project_type.save()

        assert Comment.objects.filter_project(self.project).count() == 0

    def test_field_added(self):
        self.project_type.finding_fields += [{'id': 'field_new', 'type': 'string'}]
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        get_definition(self.project_type.report_sections, 'other')['fields'].append({'id': 'field_new', 'type': 'string'})
        self.project_type.save()

        # No comment deleted or created
        assert Comment.objects.filter_project(self.project).count() == len(self.comment_paths) * 2

    def test_field_moved_to_other_section(self):
        fields_moved_ids = ['field_markdown', 'field_list', 'field_list_objects']
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        rs_other = get_definition(self.project_type.report_sections, 'other')
        fields_moved_definitions = [f for f in rs_other['fields'] if f['id'] in fields_moved_ids]
        rs_other['fields'] = [f for f in rs_other['fields'] if f['id'] not in fields_moved_ids]
        self.project_type.report_sections.append({'id': 'new', 'fields': fields_moved_definitions})
        self.project_type.save()

        for cp in self.comment_paths:
            c = Comment.objects.filter_project(self.project).filter(path=cp).first()
            assert c.section.section_id == 'new' if cp.split('.')[0] in fields_moved_definitions else 'other'

    def test_type_changed_text_range_cleared(self):
        comments = list(Comment.objects.filter_project(self.project).filter(path='field_markdown'))

        self.project_type.finding_fields = copy.deepcopy(self.project_type.finding_fields)
        next(f for f in self.project_type.finding_fields if f['id'] == 'field_markdown')['type'] = 'cvss'
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        next(f for f in next(s for s in self.project_type.report_sections if s['id'] == 'other')['fields'] if f['id'] == 'field_markdown')['type'] = 'cvss'
        self.project_type.save()

        for c in comments:
            text_original = c.text_original
            c.refresh_from_db()
            assert c.text_range is None
            assert c.text_original == text_original


@pytest.mark.django_db()
class TestPredefinedFields:
    @pytest.fixture(autouse=True)
    def setUp(self) -> None:
        self.project_type = create_project_type(
            finding_fields=serialize_field_definition(FINDING_FIELDS_CORE | FieldDefinition(fields=[FINDING_FIELDS_PREDEFINED['description']])))
        project = create_project(project_type=self.project_type)
        self.finding = create_finding(project=project)

    def test_change_structure(self):
        self.project_type.finding_fields = serialize_field_definition(self.project_type.finding_fields_obj | FieldDefinition(fields=[
            ListField(id='description', label='Changed', items=StringField(default='changed')),
        ]))
        with pytest.raises(ValidationError):
            self.project_type.clean_fields()

    def test_add_conflicting_field(self):
        self.project_type.finding_fields = serialize_field_definition(self.project_type.finding_fields_obj | FieldDefinition(fields=[
            ListField(id='recommendation', label='Changed', items=StringField(default='changed')),
        ]))
        with pytest.raises(ValidationError):
            self.project_type.clean_fields()


@pytest.mark.django_db()
class TestTemplateFieldDefinition:
    @pytest.fixture(autouse=True)
    def setUp(self):
        self.project_type1 = create_project_type(
            finding_fields=serialize_field_definition(FINDING_FIELDS_CORE | FieldDefinition(fields=[
                StringField(id='field1', default='default', label='Field 1'),
                StringField(id='field_conflict', default='default', label='Conflicting field type'),
            ])),
        )
        self.project_type2 = create_project_type(
            finding_fields=serialize_field_definition(FINDING_FIELDS_CORE| FieldDefinition(fields=[
                StringField(id='field2', default='default', label='Field 2'),
                ListField(id='field_conflict', label='Conflicting field type', items=StringField(default='default')),
            ])),
        )
        self.project_type_hidden = create_project_type(
            finding_fields=serialize_field_definition(FINDING_FIELDS_CORE | FieldDefinition(fields=[
                StringField(id='field_hidden', default='default', label='Field of hidden ProjectType'),
            ])),
        )
        project_hidden = create_project(project_type=self.project_type_hidden)
        update(self.project_type_hidden, linked_project=project_hidden)

        self.template = create_template(data={'title': 'test', 'field1': 'f1 value', 'field2': 'f2 value'})

    def test_get_template_field_definition(self):
        assert \
            set(FindingTemplate.field_definition.keys()) == \
            set(FINDING_FIELDS_CORE.keys()) | set(FINDING_FIELDS_PREDEFINED.keys()) | {'field1', 'field2', 'field_conflict'}
        assert FindingTemplate.field_definition['field_conflict'].type == FieldDataType.STRING

    def test_delete_field_definition(self):
        old_value = self.template.main_translation.data['field1']
        update(self.project_type1, finding_fields=[f for f in self.project_type1.finding_fields if f['id'] != 'field1'])
        self.template.main_translation.refresh_from_db()

        assert 'field1' not in FindingTemplate.field_definition
        assert self.template.main_translation.data_all['field1'] == old_value

    def test_change_field_type(self):
        self.project_type1.finding_fields = copy.deepcopy(self.project_type1.finding_fields)
        get_definition(self.project_type1.finding_fields, 'field1').update(
            {'type': 'list', 'label': 'changed field type', 'items': {'type': 'string', 'default': 'default'}, 'default': None},
        )
        self.project_type1.save()
        self.template.refresh_from_db()

        assert FindingTemplate.field_definition['field1'].type == FieldDataType.LIST
        assert self.template.main_translation.data['field1'] == []


@pytest.mark.django_db()
class TestReportSectionDefinition:
    @pytest.fixture(autouse=True)
    def setUp(self):
        field_definition = {'type': 'string', 'default': 'default', 'label': 'Field label'}
        self.project_type = create_project_type(
            report_sections=[
                {'id': 'section1', 'label': 'Section 1', 'fields': [{'id': 'field1'} | field_definition]},
                {'id': 'section2', 'label': 'Section 2', 'fields': [{'id': 'field2'} | field_definition]},
                {'id': 'other', 'label': 'Other', 'fields': [{'id': 'field3'} | field_definition]},
            ],
        )
        self.project = create_project(project_type=self.project_type)

    def test_add_section(self):
        self.project_type.report_sections += [{'id': 'section_new', 'fields': [{'id': 'field_new', 'type': 'string', 'default': 'default', 'label': 'new field'}]}]
        self.project_type.save()
        self.project.refresh_from_db()

        section_new = self.project.sections.get(section_id='section_new')
        assert section_new.field_definition.keys() == ['field_new']
        assert section_new.data['field_new'] == 'default'

    def test_delete_section(self):
        old_value = self.project.sections.get(section_id='section1').data['field1']
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        section1 = get_definition(self.project_type.report_sections, 'section1')
        section2 = get_definition(self.project_type.report_sections, 'section2')
        section2['fields'] += section1['fields']
        self.project_type.report_sections = [section2]
        self.project_type.save()
        self.project.refresh_from_db()

        assert not self.project.sections.filter(section_id='section1').exists()
        assert self.project.sections.get(section_id='section2').data['field1'] == old_value

    def test_move_field_to_other_section(self):
        old_value = self.project.sections.get(section_id='section1').data['field1']
        self.project_type.report_sections = copy.deepcopy(self.project_type.report_sections)
        section1 = get_definition(self.project_type.report_sections, 'section1')
        field1 = get_definition(section1['fields'], 'field1')
        section1['fields'].remove(field1)
        get_definition(self.project_type.report_sections, 'section2')['fields'].append(field1)
        self.project_type.save()
        self.project.refresh_from_db()

        assert self.project.sections.filter(section_id='section1').exists()
        assert self.project.sections.get(section_id='section2').data['field1'] == old_value


@pytest.mark.django_db()
class TestTemplateTranslation:
    @pytest.fixture(autouse=True)
    def setUp(self):
        create_project_type()  # create dummy project_type to get field_definition
        self.template = create_template(language=Language.ENGLISH_US, data={
            'title': 'Title main',
            'description': 'Description main',
            'recommendation': 'Recommendation main',
            'field_list': ['first', 'second'],
            'field_unknown': 'unknown',
        })
        self.main = self.template.main_translation
        self.trans = FindingTemplateTranslation.objects.create(template=self.template, language=Language.GERMAN_DE, title='Title translation')

    def test_template_translation_inheritance(self):
        update(self.trans, data={'title': 'Title translation', 'description': 'Description translation'})

        data_inherited = self.trans.get_data(inherit_main=True)
        assert data_inherited['title'] == self.trans.title == 'Title translation'
        assert data_inherited['description'] == 'Description translation'
        assert data_inherited['recommendation'] == self.main.data['recommendation'] == 'Recommendation main'
        assert 'recommendation' not in self.trans.data
        assert 'field_list' not in self.trans.data

    def test_template_formatting(self):
        update(self.trans, custom_fields={
            'recommendation': {'value': 'invalid format'},
            'field_list': ['first', {'value': 'invalid format'}],
            'field_object': {},
        })
        assert 'description' not in self.trans.data
        assert self.trans.data['recommendation'] is None
        assert self.trans.data['field_list'] == ['first', None]
        assert self.trans.data['field_object']['nested1'] is None

    def test_undefined_in_main(self):
        update(self.main, custom_fields={})
        data_inherited = self.trans.get_data(inherit_main=True)
        assert 'description' not in data_inherited
        assert 'field_list' not in data_inherited
        assert 'field_object' not in data_inherited

    def test_update_data(self):
        update(self.main, data={
            'title': 'new',
            'description': 'new',
        })
        data_inherited = self.trans.get_data(inherit_main=True)
        assert data_inherited['title'] != 'new'
        assert data_inherited['description'] == 'new'
        assert 'recommendation' not in data_inherited
        assert data_inherited['field_unknown'] == 'unknown'
        assert 'field_unknown' not in self.trans.data


@pytest.mark.django_db()
class TestFindingSorting:
    def assert_finding_order(self, findings_kwargs, **project_kwargs):
        findings_kwargs = reversed(self.format_findings_kwargs(findings_kwargs))
        project = create_project(
            findings_kwargs=findings_kwargs,
            **project_kwargs)
        findings_sorted = sort_findings(
            findings=[format_template_field_object(
                    {'id': str(f.id), 'created': str(f.created), 'order': f.order, **f.data},
                    definition=project.project_type.finding_fields_obj)
                for f in project.findings.all()],
            project_type=project.project_type,
            override_finding_order=project.override_finding_order,
        )
        findings_sorted_titles = [f['title'] for f in findings_sorted]
        assert findings_sorted_titles == [f'f{i + 1}' for i in range(len(findings_sorted_titles))]

    def format_findings_kwargs(self, findings_kwargs):
        for idx, finding_kwarg in enumerate(findings_kwargs):
            finding_kwarg.setdefault('data', {})
            finding_kwarg['data']['title'] = f'f{idx + 1}'
        return findings_kwargs

    def test_override_finding_order(self):
        self.assert_finding_order(override_finding_order=True, findings_kwargs=[
            {'order': 1},
            {'order': 2},
            {'order': 3},
        ])

    def test_fallback_order(self):
        self.assert_finding_order(
            override_finding_order=False,
            project_type=create_project_type(finding_ordering=[]),
            findings_kwargs=[
                {'order': 1, 'created': timezone.now() - timedelta(days=2)},
                {'order': 1, 'created': timezone.now() - timedelta(days=1)},
                {'order': 1, 'created': timezone.now() - timedelta(days=0)},
            ])

    @pytest.mark.parametrize(('finding_ordering', 'findings_kwargs'), [
        ([{'field': 'cvss', 'order': 'desc'}], [{'cvss': 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H'}, {'cvss': 'CVSS:3.1/AV:N/AC:H/PR:L/UI:R/S:C/C:L/I:L/A:L'}, {'cvss': None}]),  # CVSS
        ([{'field': 'field_string', 'order': 'asc'}], [{'field_string': 'aaa'}, {'field_string': 'bbb'}, {'field_string': 'ccc'}]),  # string field
        ([{'field': 'field_int', 'order': 'asc'}], [{'field_int': 1}, {'field_int': 10}, {'field_int': 13}]),  # number
        ([{'field': 'field_enum', 'order': 'asc'}], [{'field_enum': 'enum1'}, {'field_enum': 'enum2'}]),  # enum
        ([{'field': 'field_date', 'order': 'asc'}], [{'field_date': None}, {'field_date': '2023-01-01'}, {'field_date': '2023-06-01'}]),  # date
        ([{'field': 'field_string', 'order': 'asc'}, {'field': 'field_markdown', 'order': 'asc'}], [{'field_string': 'aaa', 'field_markdown': 'xxx'}, {'field_string': 'aaa', 'field_markdown': 'yyy'}, {'field_string': 'bbb', 'field_markdown': 'zzz'}]),  # multiple fields: string, markdown
        ([{'field': 'field_bool', 'order': 'desc'}, {'field': 'cvss', 'order': 'desc'}], [{'field_bool': True, 'cvss': 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H'}, {'field_bool': True, 'cvss': 'CVSS:3.1/AV:N/AC:H/PR:L/UI:R/S:C/C:L/I:L/A:L'}, {'field_bool': False, 'cvss': 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H'}]),  # multiple fields: -bool, -cvss
        ([{'field': 'field_enum', 'order': 'asc'}, {'field': 'field_int', 'order': 'desc'}], [{'field_enum': 'enum1', 'field_int': 2}, {'field_enum': 'enum1', 'field_int': 1}, {'field_enum': 'enum2', 'field_int': 10}, {'field_enum': 'enum2', 'field_int': 9}]),  # multiple fields with mixed asc/desc: enum, -number
    ])
    def test_finding_order_by_fields(self, finding_ordering, findings_kwargs):
        self.assert_finding_order(
            override_finding_order=False,
            project_type=create_project_type(finding_ordering=finding_ordering),
            findings_kwargs=[{'data': f} for f in findings_kwargs],
        )


@pytest.mark.django_db()
class TestFindingGrouping:
    def assert_finding_groups(self, finding_grouping, findings_kwargs, expected_groups, finding_ordering=None):
        project = create_project(
            project_type=create_project_type(finding_grouping=finding_grouping, finding_ordering=finding_ordering or []),
            findings_kwargs=findings_kwargs)
        groups = group_findings(
            findings=[format_template_field_object(
                    {'id': str(f.id), 'created': str(f.created), 'order': f.order, **f.data},
                    definition=project.project_type.finding_fields_obj)
                for f in project.findings.all()],
            project_type=project.project_type)
        group_titles = [{'label': g['label'], 'findings': [f['title'] for f in g['findings']]} for g in groups]
        assert group_titles == expected_groups

    @pytest.mark.parametrize(('finding_grouping', 'findings_kwargs', 'expected_groups'), [
        # Not grouped: everything in a single group
        (None, [{'title': 'f1'}, {'title': 'f2'}, {'title': 'f3'}], [{'label': '', 'findings': ['f1', 'f2', 'f3']}]),
        ([], [{'title': 'f1'}, {'title': 'f2'}, {'title': 'f3'}], [{'label': '', 'findings': ['f1', 'f2', 'f3']}]),
        # Group by single field
        ([{'field': 'field_enum'}], [{'title': 'f1', 'field_enum': 'enum2'}, {'title': 'f2', 'field_enum': 'enum1'}, {'title': 'f3', 'field_enum': 'enum2'}], [{'label': 'Enum Value 1', 'findings': ['f2']}, {'label': 'Enum Value 2', 'findings': ['f1', 'f3']}]),
        ([{'field': 'field_combobox'}], [{'title': 'f1', 'field_combobox': 'g1'}, {'title': 'f2', 'field_combobox': 'g2'}, {'title': 'f3', 'field_combobox': 'g1'}], [{'label': 'g1', 'findings': ['f1', 'f3']}, {'label': 'g2', 'findings': ['f2']}]),
        ([{'field': 'field_string'}], [{'title': 'f1', 'field_string': 'g1'}, {'title': 'f2', 'field_string': 'g2'}, {'title': 'f3', 'field_string': 'g1'}], [{'label': 'g1', 'findings': ['f1', 'f3']}, {'label': 'g2', 'findings': ['f2']}]),
        ([{'field': 'field_bool'}], [{'title': 'f1', 'field_bool': False}, {'title': 'f2', 'field_bool': True}, {'title': 'f3', 'field_bool': False}], [{'label': 'false', 'findings': ['f1', 'f3']}, {'label': 'true', 'findings': ['f2']}]),
        ([{'field': 'field_date'}], [{'title': 'f1', 'field_date': '2023-01-01'}, {'title': 'f2', 'field_date': '2023-06-01'}, {'title': 'f3', 'field_date': '2023-01-01'}], [{'label': '2023-01-01', 'findings': ['f1', 'f3']}, {'label': '2023-06-01', 'findings': ['f2']}]),
        ([{'field': 'field_cvss', 'order': 'desc'}], [{'title': 'f1', 'field_cvss': 'n/a'}, {'title': 'f2', 'field_cvss': 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H'}, {'title': 'f3', 'field_cvss': 'CVSS:3.1/AV:N/AC:H/PR:L/UI:R/S:C/C:N/I:N/A:N'}], [{'label': 'critical', 'findings': ['f2']}, {'label': 'info', 'findings': ['f1', 'f3']}]),
    ])
    def test_finding_grouping(self, finding_grouping, findings_kwargs, expected_groups):
        self.assert_finding_groups(
            finding_grouping=finding_grouping,
            finding_ordering=[{'field': 'title', 'order': 'asc'}],
            findings_kwargs=[{'data': f} for f in findings_kwargs],
            expected_groups=expected_groups)

    @pytest.mark.parametrize(('finding_grouping_order', 'finding_ordering_order', 'expected_groups'), [
        ('asc', 'asc', [{'label': 'g1', 'findings': ['g1f1', 'g1f2']}, {'label': 'g2', 'findings': ['g2f1', 'g2f2']}]),
        ('desc', 'desc', [{'label': 'g2', 'findings': ['g2f2', 'g2f1']}, {'label': 'g1', 'findings': ['g1f2', 'g1f1']}]),
        ('asc', 'desc', [{'label': 'g1', 'findings': ['g1f2', 'g1f1']}, {'label': 'g2', 'findings': ['g2f2', 'g2f1']}]),
        ('desc', 'asc', [{'label': 'g2', 'findings': ['g2f1', 'g2f2']}, {'label': 'g1', 'findings': ['g1f1', 'g1f2']}]),
        ('asc', None, [{'label': 'g1', 'findings': ['g1f1', 'g1f2']}, {'label': 'g2', 'findings': ['g2f1', 'g2f2']}]),
        ('desc', None, [{'label': 'g1', 'findings': ['g1f1', 'g1f2']}, {'label': 'g2', 'findings': ['g2f1', 'g2f2']}]),
    ])
    def test_group_sort(self, finding_grouping_order, finding_ordering_order, expected_groups):
        self.assert_finding_groups(
            finding_grouping=[{'field': 'field_string', 'order': finding_grouping_order}],
            finding_ordering=[{'field': 'field_int', 'order': finding_ordering_order}] if finding_ordering_order else [],
            findings_kwargs=[
                {'data': {'title': 'g1f2', 'field_string': 'g1', 'field_int': 3}, 'order': 3},
                {'data': {'title': 'g2f1', 'field_string': 'g2', 'field_int': 2}, 'order': 2},
                {'data': {'title': 'g1f1', 'field_string': 'g1', 'field_int': 1}, 'order': 1},
                {'data': {'title': 'g2f2', 'field_string': 'g2', 'field_int': 4}, 'order': 4},
            ],
            expected_groups=expected_groups,
        )


@pytest.mark.django_db()
class TestDefaultNotes:
    @pytest.fixture(autouse=True)
    def setUp(self):
        self.project_type = create_project_type()

    @pytest.mark.parametrize(('valid', 'default_notes'), [
        (True, []),
        (True, [{'id': '11111111-1111-1111-1111-111111111111', 'parent': None, 'order': 1, 'checked': True, 'icon_emoji': '🦖', 'title': 'Note title', 'text': 'Note text content'}]),
        (True, [{'id': '11111111-1111-1111-1111-111111111111', 'parent': None}, {'parent': '11111111-1111-1111-1111-111111111111'}]),
        (False, [{'parent': '22222222-2222-2222-2222-222222222222'}]),
        (False, [{'id': '11111111-1111-1111-1111-111111111111', 'parent': '11111111-1111-1111-1111-111111111111'}]),
        (False, [{'id': '11111111-1111-1111-1111-111111111111', 'parent': '22222222-2222-2222-2222-222222222222'}, {'id': '22222222-2222-2222-2222-222222222222', 'parent': '11111111-1111-1111-1111-111111111111'}]),
    ])
    def test_default_notes(self, valid, default_notes):
        # Test default_notes validation
        is_valid = True
        try:
            self.project_type.default_notes = [{
                'id': str(uuid4()),
                'parent': None,
                'order': 0,
                'checked': None,
                'icon_emoji': None,
                'title': 'Note',
                'text': 'Note text',
            } | n for n in default_notes]
            self.project_type.full_clean()
            self.project_type.save()
        except ValidationError:
            is_valid = False
        assert is_valid == valid

        # Test note created from default_notes in project
        if is_valid:
            p = create_project(project_type=self.project_type)
            for dn in self.project_type.default_notes:
                n = p.notes.get(note_id=dn['id'])
                assert (str(n.parent.note_id) if n.parent else None) == dn['parent']
                assertKeysEqual(dn, n, ['order', 'checked', 'icon_emoji', 'title', 'text'])


