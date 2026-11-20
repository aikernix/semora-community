"""Only a complete successful catalog may replace persisted structure."""
from copy import deepcopy


def completed_scan(result):
    manifest=result.get('scan_manifest') or {}
    tables=result.get('tables')
    if (result.get('status')!='completed' or manifest.get('complete') is not True
            or manifest.get('scope')!='full' or result.get('permission_gaps')
            or result.get('errors') or result.get('partial') is True
            or not isinstance(tables,list)
            or any(not isinstance(t,dict) or not isinstance(t.get('columns'),list) or not t['columns'] for t in tables)
            or manifest.get('scanned_tables')!=len(tables)):
        raise ValueError('结构扫描未完整成功，保留原结构和关联关系')
    identities=[t.get('id') or '.'.join(str(t.get(k) or '') for k in ('catalog','schema','name') if t.get(k)) for t in tables]
    if any(not k for k in identities) or len(set(identities))!=len(identities):
        raise ValueError('扫描返回重复或缺失表标识，保留原结构和关联关系')
    return deepcopy(result)
