from types import SimpleNamespace
from unittest.mock import Mock

import pydantic
import pytest

from langbot.pkg.telemetry.platform import observe_adapter


@pytest.mark.asyncio
async def test_observation_wraps_inherited_pydantic_adapter_method_once(monkeypatch):
    class API:
        async def get_file_url(self, file_id):
            return {'id': file_id}

    class Adapter(API, pydantic.BaseModel):
        def get_supported_apis(self):
            return ['get_file_url']

    record = Mock()
    monkeypatch.setattr('langbot.pkg.telemetry.platform.record', record)
    adapter = Adapter()
    ap = SimpleNamespace()
    context = SimpleNamespace()
    observe_adapter(ap, context, adapter)
    observe_adapter(ap, context, adapter)
    assert await adapter.get_file_url('file-1') == {'id': 'file-1'}
    record.assert_called_once()
