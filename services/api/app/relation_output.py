"""Public relation artifacts omit retired scoring fields without rewriting history."""
def published_relationship(candidate):
    """Research neighbors stay in checkpoints, never inside a published relation."""
    from copy import deepcopy
    return clean_relations({k: deepcopy(v) for k, v in candidate.items()
            if k not in {'other_proposals', 'target_domain_observations', 'research_key'}})


def clean_relations(value):
    if isinstance(value, list):
        return [clean_relations(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_relations(v) for k,v in value.items() if k != 'confidence'}
    return value
