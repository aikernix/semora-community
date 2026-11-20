"""Find competing mappings and shared endpoints without deciding their validity."""
from collections import defaultdict


def attach_context(candidates):
    endpoints = defaultdict(list)
    def endpoint(c, side):
        return (c[side + '_datasource_id'], c[side + '_table_id'], tuple(c[side + '_columns']))
    for c in candidates:
        for side in ('source', 'target'):
            endpoints[endpoint(c, side)].append(c)
    def describe(c):
        return {k: c[k] for k in ('id', 'source_datasource_id', 'source_table_id', 'source_columns',
                                 'target_datasource_id', 'target_table_id', 'target_columns', 'key_pairs', 'condition_template') if k in c}
    for c in candidates:
        a, b = endpoint(c, 'source'), endpoint(c, 'target')
        others, reasons = {}, set()
        for key in (a, b):
            for p in endpoints[key]:
                if p['id'] != c['id']:
                    others[p['id']] = p
        # Only the same complete field endpoint is evidence of a possible shared anchor.
        left, right = defaultdict(list), defaultdict(list)
        for key, dest in ((a, left), (b, right)):
            for p in endpoints[key]:
                if p['id'] == c['id']:
                    continue
                other = endpoint(p, 'target') if endpoint(p, 'source') == key else endpoint(p, 'source')
                dest[other].append(p)
        for key in left.keys() & right.keys():
            reasons.add('shared_endpoint_requires_direct_evidence')
            for p in left[key] + right[key]:
                others[p['id']] = p
        # Multiple proposals are context, not an automatic exclusion or ambiguity verdict.
        c['other_proposals'] = [describe(others[k]) for k in sorted(others)]
        c['global_review_reasons'] = sorted(reasons)
