"""Share observed reference domains without treating proposed edges as facts."""
from collections import defaultdict
from .relationship_inputs import digest


def domain_observations(candidates):
    by_target = defaultdict(dict)
    for c in candidates:
        profile = next((e.get('statistics', {}) for e in reversed(c['evidence']) if e.get('kind') == 'value_profile'), {})
        if not profile.get('source_nonnull_distinct_keys'):
            continue
        context = next((e for e in reversed(c['evidence']) if e.get('kind') == 'raw_schema_context'), {})
        target = (c['target_datasource_id'], c['target_table_id'], tuple(c['target_columns']))
        source = (c['source_datasource_id'], c['source_table_id'], tuple(c['source_columns']), digest(c.get('condition_template', {})))
        by_target[target][source] = {'source_table': c['source_table_id'],
            'source_definition': context.get('source_description', ''),
            'distinct_keys_checked': profile['source_nonnull_distinct_keys'],
            'matched_keys': profile.get('matched_keys', 0), 'source_exhausted': profile.get('source_exhausted', False),
            'reverse_distinct_keys_checked':profile.get('target_nonnull_distinct_keys'),
            'reverse_matched_keys':profile.get('reverse_matched_keys'),
            'sampling_method':profile.get('sampling_method')}
    return {k: [v[x] for x in sorted(v)] for k, v in by_target.items()}
