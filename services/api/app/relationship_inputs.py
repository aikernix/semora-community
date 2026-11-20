"""Raw, filtered relationship inputs and bounded, atomic research checkpoints."""
from __future__ import annotations

import hashlib
import fcntl
import tempfile
import json
import os
import re
import time
from pathlib import Path

from .relationship_context import protected_fields, relationship_columns

FORMAT_VERSION = 'raw-relations-v2-indexes'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def build_inputs(table_maps):
    result = {}
    for source_id, tables in table_maps.items():
        protected = protected_fields(list(tables.values()))
        for table_id, table in tables.items():
            key = digest([source_id, table_id])[:24]
            indexes={}
            for index in table.get('indexes') or []:
                if not index.get('columns'):
                    continue
                # Index names are storage labels; retain field order and qualification instead.
                value={k:index[k] for k in ('columns','unique','predicate','filter','filter_definition','where',
                    'condition','partial','is_partial','disabled','is_disabled','valid','is_valid','expression','expressions') if k in index}
                indexes[digest(value)]=value
            foreign_keys=[]
            for fk in table.get('foreign_keys') or []:
                target=tables.get(fk.get('target_table'))
                left,right=fk.get('columns') or [],fk.get('target_columns') or []
                available=bool(target and left and len(left)==len(right)
                    and len(set(left))==len(left) and len(set(right))==len(right)
                    and set(left)<={c['name'] for c in table.get('columns',[])}
                    and set(right)<={c['name'] for c in target.get('columns',[])})
                foreign_keys.append({**fk,'automatic_relation_available':available})
            result[key] = {
                'table_id': key, 'datasource_id': source_id, 'physical_table': table_id,
                'name': table.get('name', table_id), 'comment': table.get('description') or table.get('comment') or '',
                'kind': table.get('kind', 'TABLE'),
                'columns': [{'name': c['name'], 'type': c.get('type') or c.get('raw_type'),
                             'comment': c.get('description') or c.get('comment') or ''}
                            for c in relationship_columns(table, protected[table_id])],
                **{k: table.get(k, []) for k in ('primary_keys', 'unique_constraints')},
                'indexes':list(indexes.values()), 'foreign_keys':foreign_keys,
            }
    return result


def dialect_settings(source):
    from semora_contracts.database import PRODUCTS, baseline_pagination
    capabilities = source.scan_result.get('capabilities') or {}
    connection = source.scan_result.get('connection') or {}
    dialect = capabilities.get('dialect') or connection.get('dialect') or connection.get('compatibility_mode') or PRODUCTS.get(source.type, {}).get('dialect')
    if dialect not in {'mysql', 'postgres', 'oracle', 'tsql'}:
        raise ValueError('Unknown source dialect; validate source capabilities before relationship research')
    return dialect, capabilities.get('pagination_strategy') or baseline_pagination(dialect)


class Checkpoints:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.getenv('RELATIONSHIP_CHECKPOINT_DIR', '.cache/relationship-checkpoints'))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, job_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', job_id):
            raise ValueError('invalid_checkpoint_id')
        return self.directory / (job_id + '.json')

    def load(self, job_id, fingerprint):
        path = self.path(job_id)
        if path.exists():
            value = json.loads(path.read_text())
            if value.get('fingerprint') == fingerprint:
                return value
        return {'fingerprint': fingerprint, 'discovery': {}, 'research': {}, 'metrics': {'calls': 0, 'cache_hits': 0}}

    def save(self, job_id, value):
        path = self.path(job_id)
        # Separate processes may review the same source; use unique temp files and merge shared caches.
        with path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if job_id.startswith('cache_') and path.exists():
                previous = json.loads(path.read_text())
                if previous.get('fingerprint') == value.get('fingerprint'):
                    value = {**value, **{k: {**previous.get(k, {}), **value.get(k, {})} for k in ('discovery', 'research')}}
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.directory, prefix=job_id, suffix='.tmp', delete=False) as target:
                json.dump(value, target, ensure_ascii=False, default=str)
                target.flush()
                os.fsync(target.fileno())
                temp = Path(target.name)
            try:
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)


    def cleanup(self):
        for path in self.directory.glob('*.json'):
            try:
                value = json.loads(path.read_text())
                if value.get('terminal_at', time.time()) < time.time() - 7 * 86400:
                    path.unlink()
            except (OSError, ValueError):
                continue


def save_inputs(repository, inputs, source_ids=None):
    if not repository.dsn:
        return
    with repository._connect() as connection, connection.transaction():
        write_inputs(connection,inputs,source_ids)


def write_inputs(connection, inputs, source_ids=None):
    for card in inputs.values():
        connection.execute('''INSERT INTO semora_meta.datasource_relationship_inputs
            (datasource_id,table_id,input_hash,format_version,payload)
            VALUES(%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT(datasource_id,table_id) DO UPDATE SET input_hash=excluded.input_hash,
            format_version=excluded.format_version,payload=excluded.payload,updated_at=now()
            WHERE datasource_relationship_inputs.input_hash<>excluded.input_hash OR datasource_relationship_inputs.format_version<>excluded.format_version''',
            (card['datasource_id'], card['physical_table'], digest(card), FORMAT_VERSION, json.dumps(card, ensure_ascii=False)))
    for source_id in set(source_ids or ()) | {v['datasource_id'] for v in inputs.values()}:
        connection.execute('DELETE FROM semora_meta.datasource_relationship_inputs WHERE datasource_id=%s AND NOT(table_id=ANY(%s))',
                           (source_id, [v['physical_table'] for v in inputs.values() if v['datasource_id'] == source_id]))
