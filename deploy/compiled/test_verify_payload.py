"""Packaging regressions: fail closed on common source leaks, not on runtime assets."""
import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('delivery_verifier', Path(__file__).with_name('verify_payload.py'))
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


@pytest.fixture
def payload(tmp_path):
    package = tmp_path / 'app'
    package.mkdir()
    (package / '__init__.py').write_bytes(b'from ._compiled_init import *\n')
    suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
    for name in ('_compiled_init', 'workflow'):
        (package / (name + suffix)).write_bytes(b'native fixture')
    manifest = {'modules': ['app._compiled_init', 'app.workflow'], 'assets': {}}
    (tmp_path / 'compiled-manifest.json').write_text(json.dumps(manifest))
    return tmp_path


def test_native_only_payload_allowed(payload):
    verifier.verify(payload)


@pytest.mark.parametrize('name', ['workflow.py', 'workflow.pyc', 'workflow.c', 'workflow.pyx', '.env', '.git/config'])
def test_source_and_secret_leaks_rejected(payload, name):
    path = payload / 'app' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('must not ship')
    with pytest.raises(RuntimeError, match='Unexpected file'):
        verifier.verify(payload)


def test_source_outside_package_rejected(payload):
    (payload / 'source-backup.py').write_text('must not ship')
    with pytest.raises(RuntimeError, match='Unexpected runtime payload'):
        verifier.verify(payload)


def test_modified_loader_rejected(payload):
    (payload / 'app/__init__.py').write_text('from ._compiled_init import *\nSECRET = "leaked"\n')
    with pytest.raises(RuntimeError, match='Unexpected file'):
        verifier.verify(payload)


def test_missing_native_module_rejected(payload):
    next((payload / 'app').glob('workflow.*')).unlink()
    with pytest.raises(RuntimeError, match='Missing native module'):
        verifier.verify(payload)


def test_reviewed_asset_allowed_and_tampering_rejected(payload):
    path = payload / 'app/vendor/wren-mdl.schema.json'
    path.parent.mkdir()
    path.write_bytes(b'{}')
    manifest_path = payload / 'compiled-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['assets'] = {'app/vendor/wren-mdl.schema.json': hashlib.sha256(b'{}').hexdigest()}
    manifest_path.write_text(json.dumps(manifest))
    verifier.verify(payload)
    path.write_bytes(b'changed')
    with pytest.raises(RuntimeError, match='Unexpected file'):
        verifier.verify(payload)


def test_manifest_cannot_allow_arbitrary_sources(payload):
    manifest_path = payload / 'compiled-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['assets'] = {'app/workflow.py': 'irrelevant'}
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match='Unrecognized runtime asset'):
        verifier.verify(payload)
