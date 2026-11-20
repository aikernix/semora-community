"""Additional opinions are reserved for unresolved or contradictory evidence."""
from .relationship_rules import MIN_REFERENCE_COVERAGE

def review_reasons(candidate, decision):
    if decision.get('query_outcome') == 'exclude':
        return []
    reasons = []
    if decision.get('query_outcome') != 'usable' or decision.get('missing_evidence'):
        reasons.append('evidence_unresolved')
    if decision.get('alternative_unresolved'):
        reasons.append('competing_targets')
    if decision.get('key_domain') != 'same':
        reasons.append('identifier_domain_unresolved')
    if decision.get('relationship_kind') in {'shared_entity', 'unknown', None}:
        reasons.append('direct_reference_unresolved')
    if not decision.get('supported'):
        reasons.append('semantic_uncertainty')
    if candidate.get('global_review_reasons'):
        reasons.extend(candidate['global_review_reasons'])
    if decision.get('relationship_kind') == 'identity':
        reasons.append('record_identity_requires_review')
    key_context = next((e for e in reversed(candidate['evidence']) if e.get('kind') == 'target_key_context'), {})
    if (candidate.get('source_columns') == candidate.get('target_columns')
            and key_context.get('primary_key') is False
            and not any(e.get('kind') == 'explicit_field_reference' for e in candidate['evidence'])):
        reasons.append('shared_identifier_requires_role_review')
    profiles = [e.get('statistics', {}) for e in candidate['evidence'] if e.get('kind') == 'value_profile']
    if profiles:
        p = profiles[-1]
        if p.get('target_context'):
            reasons.append('target_role_context_requires_review')
        if p.get('target_context_errors'):
            reasons.append('target_role_context_unavailable')
        from .relationship_profile import support
        facts = support(p, candidate.get('scope') == 'cross_source')
        if p.get('method') == 'bidirectional_random_membership_v4':
            if not facts['positive_matches']:
                reasons.append('no_observed_matches')
            elif not facts['high_coverage']:
                reasons.append('partial_coverage_requires_explanation')
        elif p.get('source_nonnull_distinct_keys', 0) and p.get('directional_coverage', 0) < MIN_REFERENCE_COVERAGE:
            reasons.append('data_contradiction')
    contracts = [e for e in candidate['evidence'] if e.get('kind') == 'join_contract']
    if contracts and not contracts[-1].get('valid'):
        reasons.append('join_conditions_unresolved')
    return sorted(set(reasons))


def structure_is_sufficient(candidate, decision):
    """Semantic clarity is usable only with already verified coverage evidence."""
    from .relationship_profile import support
    profile = next((e.get('statistics', {}) for e in reversed(candidate.get('evidence', [])) if e.get('kind') == 'value_profile'), {})
    if not support(profile, candidate.get('scope') == 'cross_source')['high_coverage']:
        return False
    if decision.get('query_outcome') != 'usable' or not decision.get('supported'):
        return False
    if not decision.get('structure_sufficient') or not str(decision.get('reason') or '').strip():
        return False
    if review_reasons(candidate, decision):
        return False
    if decision.get('relationship_kind') not in {'entity_reference', 'direct_record_reference', 'identity'}:
        return False
    if decision.get('target_role') not in {'reference', 'identity'}:
        return False
    if decision.get('additional_predicates', []) != candidate.get('condition_template', {}).get('guards', []):
        return False
    # Multiple possible targets require data checks, but are not automatically invalid.
    for other in candidate.get('other_proposals', []):
        same_source = (other.get('source_datasource_id') == candidate.get('source_datasource_id')
                       and other.get('source_table_id') == candidate.get('source_table_id')
                       and other.get('source_columns') == candidate.get('source_columns'))
        different_target = (other.get('target_datasource_id'), other.get('target_table_id'), other.get('target_columns')) != (
            candidate.get('target_datasource_id'), candidate.get('target_table_id'), candidate.get('target_columns'))
        if same_source and different_target:
            return False
    return True
