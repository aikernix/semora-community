"""Exercise native imports, request signatures, and SQL functionality without credentials."""
import asyncio
import importlib
import importlib.machinery
import inspect
import json
from pathlib import Path
import sys
import urllib.request
import urllib.error

import uvicorn



service = sys.argv[1]
manifest = json.loads(Path('/app/compiled-manifest.json').read_text())
for name in manifest['modules']:
    module = importlib.import_module(name)
    assert isinstance(module.__loader__, importlib.machinery.ExtensionFileLoader), name

from app.main import app

async def check_http():
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=18123, log_level='warning'))
    task = asyncio.create_task(server.serve())
    async def request(path, body=None):
        def send():
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request('http://127.0.0.1:18123' + path, data=data,
                                         headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                return exc.code, None
        return await asyncio.to_thread(send)
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
                raise RuntimeError('Server exited before startup')
            await asyncio.sleep(.05)
        assert server.started, 'Native runtime startup timed out'
        assert (await request('/healthz'))[0] == 200
        status, schema = await request('/openapi.json')
        assert status == 200 and schema['paths']
        if service == 'api':
            assert (await request('/api/v1/model/status'))[0] == 200
            assert (await request('/api/v1/sessions', {'title': '', 'domain': 'test'}))[0] == 422
            import httpx
            from semora_agent.model_diagnostics import RequestDiagnostics
            diagnostic = RequestDiagnostics({}, lambda *args: None)
            async with httpx.AsyncClient() as client:
                response = await client.get('http://127.0.0.1:18123/healthz',
                    extensions={'trace': diagnostic.transport_trace})
            assert response.status_code == 200
            assert 'tcp_connected_ms' in diagnostic.data
    finally:
        server.should_exit = True
        await task


asyncio.run(check_http())
if service == 'api':
    from app.main import list_domains
    assert inspect.iscoroutinefunction(list_domains)
    try:
        inspect.getsource(list_domains)
    except (OSError, TypeError):
        pass
    else:
        raise AssertionError('Original endpoint source is readable')
    from app.result_engine import local_query
    from semora_contracts import SQLProgramStep
    value = SQLProgramStep(name='check', purpose='Check', data_source_id='source',
                           sql='SELECT id FROM items', output_columns=['id'])
    assert value.model_dump()['output_columns'] == ['id']
    assert callable(local_query)
else:
    from app.main import assert_readonly
    assert_readonly('SELECT 1 LIMIT 1', 'postgres')
    try:
        assert_readonly('DELETE FROM items', 'postgres')
    except Exception:
        pass
    else:
        raise AssertionError('Write query accepted')

print(f'{service}: native imports, HTTP routing, OpenAPI and contracts passed')
