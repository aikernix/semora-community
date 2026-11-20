"""Fenced task work and keyword-set commits; production persistence is PostgreSQL."""
from __future__ import annotations

import json
from uuid import uuid4


class WorkBusy(ValueError):
    pass


def job_input_hash(repository, job):
    value=job.get('details',{}).get('analysis_input_hash')
    if value or not repository.dsn:
        return value
    # Older keyword jobs did not persist this pointer; recover their original input namespace.
    with repository._connect() as connection:
        row=connection.execute('''SELECT input_hash FROM semora_meta.relationship_analysis_work
            WHERE job_id=%s ORDER BY (kind='stage' AND status='completed') DESC,updated_at DESC LIMIT 1''',
            (job['id'],)).fetchone()
    return row[0] if row else None


class RelationshipWork:
    def __init__(self, repository, job_id, input_hash, state, save):
        self.repository, self.job_id, self.input_hash = repository, job_id, input_hash
        self.state, self.save = state, save
        self.owner = uuid4().hex
        # File-backed checkpoints are used by isolated/offline repositories only.
        self.local = state.setdefault('keyword_work', {}).setdefault(input_hash, {})

    @property
    def database(self):
        return self.repository is not None and bool(self.repository.dsn)

    def get(self, kind, key):
        if not self.database:
            return self.local.get(kind + ':' + key)
        with self.repository._connect() as connection:
            row = connection.execute('''SELECT status,payload FROM semora_meta.relationship_analysis_work
                WHERE job_id=%s AND input_hash=%s AND kind=%s AND work_key=%s''',
                (self.job_id, self.input_hash, kind, key)).fetchone()
        return {'status': row[0], 'payload': row[1]} if row else None

    def claim(self, kind, key):
        if not self.database:
            old = self.get(kind, key)
            if old and old['status'] == 'completed':
                return False
            self.local[kind + ':' + key] = {'status': 'running', 'owner': self.owner}
            self.save()
            return True
        with self.repository._connect() as connection:
            row = connection.execute('''INSERT INTO semora_meta.relationship_analysis_work
                (job_id,input_hash,kind,work_key,owner,lease_until)
                VALUES(%s,%s,%s,%s,%s,now()+interval '20 minutes')
                ON CONFLICT(job_id,input_hash,kind,work_key) DO UPDATE SET
                  status='running',owner=excluded.owner,lease_until=excluded.lease_until,updated_at=now()
                WHERE relationship_analysis_work.status='failed'
                   OR (relationship_analysis_work.status='running' AND relationship_analysis_work.lease_until<now())
                RETURNING owner''', (self.job_id,self.input_hash,kind,key,self.owner)).fetchone()
        if row:
            return True
        if (self.get(kind, key) or {}).get('status') == 'completed':
            return False
        raise WorkBusy('relationship_work_already_running')

    def finish(self, kind, key, payload, keywords=(), *, remove_keywords=()):
        if not self.database:
            self.local[kind + ':' + key] = {'status': 'completed', 'payload': payload}
            values = self.local.setdefault('keywords', {})
            values.update((normalize_keyword(k), k) for k in keywords)
            for keyword in remove_keywords:
                values.pop(normalize_keyword(keyword),None)
            self.save()
            return
        with self.repository._connect() as connection, connection.transaction():
            row = connection.execute('''UPDATE semora_meta.relationship_analysis_work
                SET status='completed',payload=%s::jsonb,lease_until=NULL,updated_at=now()
                WHERE job_id=%s AND input_hash=%s AND kind=%s AND work_key=%s
                  AND owner=%s AND status='running' RETURNING work_key''',
                (json.dumps(payload,ensure_ascii=False),self.job_id,self.input_hash,kind,key,self.owner)).fetchone()
            if not row:
                raise WorkBusy('relationship_work_lease_lost')
            # Keyword set and the successful batch marker commit in the same transaction.
            for keyword in keywords:
                connection.execute('''INSERT INTO semora_meta.relationship_analysis_keywords
                    (job_id,input_hash,normalized_keyword,keyword) VALUES(%s,%s,%s,%s)
                    ON CONFLICT(job_id,input_hash,normalized_keyword) DO NOTHING''',
                    (self.job_id,self.input_hash,normalize_keyword(keyword),keyword))

            if remove_keywords:
                connection.execute('''DELETE FROM semora_meta.relationship_analysis_keywords
                    WHERE job_id=%s AND input_hash=%s AND normalized_keyword=ANY(%s)''',
                    (self.job_id,self.input_hash,[normalize_keyword(k) for k in remove_keywords]))

    def fail(self, kind, key, code, *, details=None):
        payload={'error':code,**({'diagnostic':details} if details else {})}
        if not self.database:
            self.local[kind + ':' + key] = {'status': 'failed', 'payload': payload}
            self.save()
            return
        with self.repository._connect() as connection:
            connection.execute('''UPDATE semora_meta.relationship_analysis_work
                SET status='failed',payload=%s::jsonb,lease_until=NULL,updated_at=now()
                WHERE job_id=%s AND input_hash=%s AND kind=%s AND work_key=%s AND owner=%s AND status='running' ''',
                (json.dumps(payload,ensure_ascii=False),self.job_id,self.input_hash,kind,key,self.owner))

    def keywords(self):
        if not self.database:
            return sorted(self.local.get('keywords', {}).values(), key=normalize_keyword)
        with self.repository._connect() as connection:
            rows = connection.execute('''SELECT keyword FROM semora_meta.relationship_analysis_keywords
                WHERE job_id=%s AND input_hash=%s ORDER BY normalized_keyword''',
                (self.job_id,self.input_hash)).fetchall()
        return [r[0] for r in rows]

    def retry_completed(self, kind, key):
        """Reopen only explicitly incomplete cached work, never invalidate sibling requests."""
        if not self.database:
            record=self.get(kind,key)
            if record and record['status']=='completed':
                record['status']='failed'
                self.save()
            return
        with self.repository._connect() as connection:
            connection.execute('''UPDATE semora_meta.relationship_analysis_work
                SET status='failed',lease_until=NULL,updated_at=now()
                WHERE job_id=%s AND input_hash=%s AND kind=%s AND work_key=%s AND status='completed' ''',
                (self.job_id,self.input_hash,kind,key))

    def cleanup(self):
        if not self.database:
            return
        with self.repository._connect() as connection, connection.transaction():
            for table in ('relationship_analysis_work','relationship_analysis_keywords'):
                connection.execute(f'''DELETE FROM semora_meta.{table} w USING semora_meta.datasource_onboarding_jobs j
                    WHERE w.job_id=j.id AND j.id<>%s AND j.status=ANY(%s)
                      AND j.completed_at<now()-interval '7 days' ''',
                    (self.job_id,['completed','completed_with_pending','failed','cancelled']))

    def recover(self):
        """Call only after the owning job is known interrupted, never from a result read."""
        if not self.database:
            for value in self.local.values():
                if isinstance(value,dict) and value.get('status')=='running': value['status']='failed'
            self.save()
            return
        with self.repository._connect() as connection:
            connection.execute('''UPDATE semora_meta.relationship_analysis_work SET status='failed',
                owner=NULL,lease_until=NULL,updated_at=now()
                WHERE job_id=%s AND input_hash=%s AND status='running' ''',(self.job_id,self.input_hash))


def normalize_keyword(value):
    return ''.join(value.casefold().split())


GENERIC_KEYWORDS = frozenset({'id','name','code','type','status','date','time','key','value',
    'table','column','field','编号','名称','类型','状态','字段','表','主键','外键',
    'varchar','nvarchar','text','int','integer','bigint','number','decimal','json','boolean'})


def keyword_set(values):
    # Only exact generic words are blocked. Short business abbreviations remain eligible.
    result = {}
    for value in values:
        normalized = normalize_keyword(value)
        if not normalized or normalized in GENERIC_KEYWORDS:
            raise ValueError('non_business_search_keyword')
        result.setdefault(normalized, ' '.join(value.split()))
    return list(result.values())
