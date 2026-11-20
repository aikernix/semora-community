"""Normalize complete joins without making semantic reference decisions."""
import re
from collections import defaultdict
from .relationship_inputs import digest


def join_signature(candidate):
    pairs = sorted(tuple(sorted(((candidate['source_datasource_id'], candidate['source_table_id'], p['source']),
                                  (candidate['target_datasource_id'], candidate['target_table_id'], p['target']))))
                   for p in candidate['key_pairs'])
    guards = sorted((candidate[g['side']+'_datasource_id'], candidate[g['side']+'_table_id'],
                     g['field'], g.get('operator', 'eq'), digest(g['value'])) for g in candidate.get('condition_template', {}).get('guards', []))
    return digest([candidate.get('domain') or '', pairs, guards])


def unconditional_unique_keys(card):
    keys = card.get('primary_keys', []) + card.get('unique_constraints', []) + [k for k in card.get('indexes', []) if k.get('unique')]
    return [k['columns'] for k in keys if k.get('columns')
            and not any(k.get(v) for v in ('predicate', 'filter', 'filter_definition', 'where', 'condition', 'partial', 'is_partial', 'disabled', 'is_disabled','expression','expressions'))
            and k.get('valid', True) is not False and k.get('is_valid', True) is not False]


def unique_key(card, fields):
    return any({c.casefold() for c in key} <= {c.casefold() for c in fields} for key in unconditional_unique_keys(card))


def orient_reference(value, cards):
    a, b = cards[value['source_table_id']], cards[value['target_table_id']]
    pairs = value['key_pairs']
    if (unique_key(a, [p['source'] for p in pairs])
            and not unique_key(b, [p['target'] for p in pairs])):
        return {**value, 'source_table_id': value['target_table_id'], 'target_table_id': value['source_table_id'],
                'key_pairs': [{'source': p['target'], 'target': p['source']} for p in pairs],
                'guards': [{**g, 'side': 'target' if g['side'] == 'source' else 'source'} for g in value.get('guards', [])],
                'direction_normalized': True}
    return value


def structural_families(cards):
    """Same numeric table family AND complete shape; similarity is only a lead."""
    groups = defaultdict(list)
    for identifier, card in cards.items():
        name = card.get('name', '').rsplit('.', 1)[-1]
        base = re.sub(r'[_-]\d+$', '', name)
        shape = [card.get('kind'), card.get('columns'),
                 [k.get('columns') for k in card.get('primary_keys', [])],
                 sorted([sorted(k.get('columns', [])) for k in card.get('unique_constraints', [])])]
        groups[(card.get('datasource_id'), base, digest(shape))].append(identifier)
    return [sorted(ids) for ids in groups.values() if len(ids) > 1]


def expand_family_leads(proposals, cards):
    families = {identifier: group for group in structural_families(cards) for identifier in group}
    def identity(p):
        return digest([p['source_table_id'], p['target_table_id'],
                       sorted((v['source'], v['target']) for v in p['key_pairs']),
                       sorted(p.get('guards', []), key=digest)])
    result = {identity(p): p for p in proposals}
    for p in proposals:
        group = families.get(p['source_table_id'], [])
        # Do not infer routing into target partitions or join sibling partitions.
        if p['target_table_id'] in group:
            continue
        for identifier in group:
            if identifier == p['source_table_id']:
                continue
            sibling = {**p, 'source_table_id': identifier,
                       'family_lead_from': p['source_table_id']}
            result.setdefault(identity(sibling), sibling)
    return list(result.values())
