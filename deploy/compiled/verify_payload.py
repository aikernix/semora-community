"""Fail builds when owned source/code-generation files leak into runtime payload."""
from __future__ import annotations

import importlib.machinery
import hashlib
import json
from pathlib import Path
import sys


def verify(root: Path) -> None:
    manifest = json.loads((root / 'compiled-manifest.json').read_text())
    packages = sorted({name.split('.')[0] for name in manifest['modules']})
    assets = manifest.get('assets', {})
    known_assets = {'app/vendor/README.md', 'app/vendor/WREN-LICENSE-APACHE-2.0', 'app/vendor/wren-mdl.schema.json'}
    if not set(assets) <= known_assets:
        raise RuntimeError('Unrecognized runtime asset')
    for path in root.iterdir():
        if path.name not in {*packages, 'compiled-manifest.json'}:
            raise RuntimeError(f'Unexpected runtime payload: {path.name}')
    allowed_loader = b'from ._compiled_init import *\n'
    for package in packages:
        for path in (root / package).rglob('*'):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative in assets and hashlib.sha256(path.read_bytes()).hexdigest() == assets[relative]:
                continue
            if path.name == '__init__.py' and path.read_bytes() == allowed_loader:
                continue
            if any(path.name.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES):
                continue
            raise RuntimeError(f'Unexpected file in compiled package: {path.relative_to(root)}')
    for name in manifest['modules']:
        path = root.joinpath(*name.split('.'))
        if not any(Path(str(path) + suffix).is_file() for suffix in importlib.machinery.EXTENSION_SUFFIXES):
            raise RuntimeError(f'Missing native module: {name}')
    print(f"Verified {len(manifest['modules'])} native modules; no owned implementation sources or bytecode")


if __name__ == '__main__':
    verify(Path(sys.argv[1] if len(sys.argv) > 1 else '/app'))
