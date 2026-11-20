"""Pure mapping comparison helpers for relationship regression tests."""
from app.relationship_normalization import join_signature


def normalized(r, defaults):
    sid = r.get('source_datasource_id', defaults.get('datasource_id', ''))
    return {'source_datasource_id': sid, 'source_table_id': r['source_table_id'],
            'target_datasource_id': r.get('target_datasource_id', defaults.get('datasource_id', sid)),
            'target_table_id': r['target_table_id'], 'key_pairs': r['key_pairs'],
            'condition_template': r.get('condition_template', {'guards': r.get('guards', r.get('predicates', []))}),
            'domain': r.get('domain', defaults.get('domain'))}


def compare(reference, runs):
    cases = {join_signature(normalized(c, reference)): c for c in reference['cases']}
    results, valid = [], []
    for index, run in enumerate(runs):
        stage = run.get('stage')
        if stage not in {'proposed', 'accepted'}:
            raise ValueError('Each run must declare stage=proposed or stage=accepted')
        entries = {join_signature(normalized(r, {**reference, **run})): r for r in run.get('relations', [])}
        scope = set(run.get('table_scope', [])); sources = set(run.get('source_scope', []))
        eligible = {k: c for k, c in cases.items() if (not scope or {c['source_table_id'], c['target_table_id']} <= scope)
                    and (not sources or c['source_table_id'] in sources or c['target_table_id'] in sources)}
        positives = {k for k, c in eligible.items() if c['expected'] == 'accept'}
        forbidden = {k for k, c in eligible.items() if c['expected'] == 'reject'}
        unresolved = {k for k, c in eligible.items() if c['expected'] == 'needs_evidence'}
        error = run.get('technical_error')
        result = {'run': run.get('name', str(index)), 'stage': stage, 'technical_error': error,
                  'unique_mapping_count': len(entries), 'known_positive_count': len(positives),
                  'positive_hits': [eligible[k] for k in sorted(positives & entries.keys())],
                  'missing_positives': [eligible[k] for k in sorted(positives - entries.keys())] if not error else [],
                  'known_negative_present': [eligible[k] for k in sorted(forbidden & entries.keys())],
                  'unproven_present': [eligible[k] for k in sorted(unresolved & entries.keys())],
                  'unclassified_extras': [entries[k] for k in sorted(entries.keys() - cases.keys())]}
        result['passed_reference_cases'] = (not error and not result['missing_positives']
            and not result['known_negative_present']
            and (stage == 'proposed' or not result['unproven_present']))
        results.append(result)
        if not error:
            identity = tuple(run.get(k) for k in ('model', 'input_fingerprint', 'prompt_fingerprint', 'rule_version')) + (stage,)
            valid.append((identity, entries, bool(all(identity[:-1]))))
    differences = []
    for i in range(1, len(valid)):
        before, after = valid[i - 1], valid[i]
        differences.append({'same_request_identity': before[2] and after[2] and before[0] == after[0],
                            'added': [after[1][k] for k in sorted(after[1].keys() - before[1].keys())],
                            'removed': [before[1][k] for k in sorted(before[1].keys() - after[1].keys())]})
    comparable = len(valid) >= 2 and all(d['same_request_identity'] for d in differences)
    return {'runs': results, 'mapping_differences': differences,
            'same_request_stable': all(not d['added'] and not d['removed'] for d in differences) if comparable else None,
            'note': 'Proposals are not approved relations. Unclassified extras are not automatically errors. Technical failures are not negative findings.'}
