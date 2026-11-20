"""Build only Semora-owned modules; never copy original sources to delivery output."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

from Cython.Build import cythonize
from setuptools import Extension, setup


SOURCE = Path('/source')
OUTPUT = Path('/compiled')
SERVICE = os.environ['SEMORA_BUILD_SERVICE']
if SERVICE not in {'api', 'executor'}:
    raise SystemExit('Unsupported build service')

packages = ['app', 'semora_telemetry', 'semora_contracts']
if SERVICE == 'api':
    packages += ['semora_agent']

extensions = []
source_hashes = {}
for package in packages:
    root = SOURCE / package
    if not (root / '__init__.py').is_file():
        raise SystemExit(f'Missing package: {package}')
    for source in sorted(root.rglob('*.py')):
        relative = source.relative_to(SOURCE)
        source_hashes[str(relative)] = hashlib.sha256(source.read_bytes()).hexdigest()
        # Tiny generated package loaders contain no original application logic.
        # Moving initializer code into an extension preserves package imports.
        if source.name == '__init__.py':
            compiled_source = source.with_name('_compiled_init.py')
            if compiled_source.exists():
                raise SystemExit(f'Reserved module name: {compiled_source}')
            shutil.copyfile(source, compiled_source)
            module = '.'.join(relative.parent.parts) + '._compiled_init'
            target = OUTPUT / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('from ._compiled_init import *\n', encoding='utf-8')
        else:
            compiled_source = source
            module = '.'.join(relative.with_suffix('').parts)
        extensions.append(Extension(
            module, [str(compiled_source)],
            extra_compile_args=['-O2', '-g0'], extra_link_args=['-s'],
        ))

setup(
    name=f'semora-{SERVICE}-compiled',
    ext_modules=cythonize(
        extensions, build_dir='/build/cython', nthreads=1,
        compiler_directives={
            'language_level': 3,
            'annotation_typing': False,
            'infer_types': False,
            'binding': True,
            'emit_code_comments': False,
        },
    ),
    script_args=['build_ext', '--build-lib', str(OUTPUT), '--build-temp', '/build/objects', '-j', '1'],
)
vendor = SOURCE / 'app' / 'vendor'
assets = {}
if SERVICE == 'api' and vendor.exists():
    for name in ('README.md', 'WREN-LICENSE-APACHE-2.0', 'wren-mdl.schema.json'):
        source = vendor / name
        if not source.is_file():
            raise SystemExit(f'Missing runtime asset: {name}')
        target = OUTPUT / 'app' / 'vendor' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        assets[str(target.relative_to(OUTPUT))] = hashlib.sha256(target.read_bytes()).hexdigest()

(OUTPUT / 'compiled-manifest.json').write_text(json.dumps({
    'service': SERVICE, 'modules': [extension.name for extension in extensions],
    'source_sha256': source_hashes,
    'assets': assets,
}, indent=2), encoding='utf-8')
