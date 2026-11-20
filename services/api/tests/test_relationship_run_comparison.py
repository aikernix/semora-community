from relationship_comparison import compare


def edge(field):
    return {'source_table_id': 'events', 'target_table_id': 'registry',
            'key_pairs': [{'source': field, 'target': 'id'}]}


def run(*relations, **extra):
    return {'relations': list(relations), 'stage': 'accepted', 'model': 'model',
            'input_fingerprint': 'input', 'prompt_fingerprint': 'prompt', 'rule_version': 'rules', **extra}


def test_equal_counts_cannot_hide_different_or_forbidden_mappings():
    baseline = {'cases': [{**edge('owner_ref'), 'expected': 'accept'},
                          {**edge('shared_subject'), 'expected': 'reject'}]}
    result = compare(baseline, [run(edge('owner_ref')), run(edge('shared_subject'))])
    assert result['runs'][0]['passed_reference_cases']
    assert not result['runs'][1]['passed_reference_cases']
    assert len(result['runs'][1]['missing_positives']) == 1
    assert len(result['mapping_differences'][0]['added']) == 1
    assert result['same_request_stable'] is False


def test_role_wording_does_not_change_mapping_stability():
    baseline = {'cases': [{**edge('ref'), 'expected': 'accept'}]}
    result = compare(baseline, [run({**edge('ref'), 'query_role': 'owner'}),
                                run({**edge('ref'), 'query_role': 'lookup'})])
    assert result['same_request_stable'] is True


def test_failed_or_different_prompt_runs_do_not_prove_repeatability():
    baseline = {'cases': [{**edge('ref'), 'expected': 'accept'}]}
    result = compare(baseline, [run(edge('ref')), run(technical_error='incomplete_response')])
    assert result['same_request_stable'] is None
    assert result['runs'][1]['missing_positives'] == []
    result = compare(baseline, [run(edge('ref')), run(edge('ref'), prompt_fingerprint='changed')])
    assert result['same_request_stable'] is None


def test_proposal_stage_can_retain_uncertainty_but_not_known_false_edges():
    baseline = {'cases': [{**edge('shared'), 'expected': 'reject'},
                          {**edge('unclear'), 'expected': 'needs_evidence'}]}
    result = compare(baseline, [run(edge('shared'), stage='proposed'),
                                run(edge('unclear'), stage='proposed'), run(edge('unclear'))])
    assert [r['passed_reference_cases'] for r in result['runs']] == [False, True, False]
