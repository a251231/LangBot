"""Identifier normalization must not rely on SQLite's permissive codecs."""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langbot.pkg.api.http.service import monitoring
from langbot.pkg.api.http.context import ExecutionContext


@pytest.mark.parametrize(
    ('value', 'expected'),
    [(None, None), ('', ''), ('00123', '00123'), ('  用户  ', '  用户  '), (123, '123'), (-123, '-123'), (0, '0')],
)
def test_normalize_user_id_preserves_opaque_strings(value, expected):
    assert monitoring._normalize_user_id(value) == expected


@pytest.mark.parametrize('value', [True, False, 1.5, b'123', ['123'], {'id': 123}])
def test_normalize_user_id_rejects_unsupported_types(value):
    with pytest.raises(TypeError, match='user_id must be a string, integer, or None'):
        monitoring._normalize_user_id(value)


@pytest.mark.asyncio
@pytest.mark.parametrize('user_id', [123, -123, 0, '00123', '  opaque  '])
async def test_session_activity_normalizes_user_id_before_database_binding(user_id):
    execute = AsyncMock(return_value=SimpleNamespace(rowcount=1))
    service = monitoring.MonitoringService(SimpleNamespace(persistence_mgr=SimpleNamespace(execute_async=execute)))
    context = ExecutionContext(instance_uuid='test', workspace_uuid='workspace', placement_generation=1, bot_uuid='bot')

    assert await service.update_session_activity(context, 'person_42', user_id=user_id, user_name='Alice')

    params = execute.call_args.args[0].compile().params
    assert params['user_id'] == str(user_id)
    assert isinstance(params['user_id'], str)
    assert params['user_name'] == 'Alice'
