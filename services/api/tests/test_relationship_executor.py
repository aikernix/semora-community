import pytest
from services.executor.app import main as executor


@pytest.mark.parametrize("kind,table", [("mysql", "public.projection"), ("postgresql", "public.projection")])
def test_definition_reader_obeys_the_same_limit_requirement_as_business_queries(monkeypatch, kind, table):
    def query(body, token=None):
        executor.assert_readonly(body.sql, "mysql" if kind == "mysql" else "postgres")
        assert "LIMIT 1" in body.sql
        return {"rows": [["SELECT id FROM public.entities"]]}
    monkeypatch.setattr(executor, "execute_query", query)
    result = executor.relation_definitions(executor.RelationDefinitionInput(
        datasource_id="source", type=kind, secret_ref="DATASOURCE_SOURCE_URL", table_id=table))
    assert result["status"] == "available"


def test_membership_validation_deduplicates_rows_and_limits_every_query(monkeypatch):
    from sqlglot import parse_one, exp
    calls = []
    def query(body, token=None):
        executor.assert_readonly(body.sql, "mysql")
        import re
        tree = parse_one(re.sub(r"%\([A-Za-z0-9_]+\)s", "NULL", body.sql), read="mysql")
        calls.append(tree)
        assert tree.find(exp.Limit)
        if "events" in body.sql:
            return {"rows": [[i % 12] for i in range(36)]}
        return {"rows": [[i] for i in range(12)]}
    monkeypatch.setattr(executor, "execute_query", query)
    source = executor.DataSourceInput(datasource_id="source", type="mysql", secret_ref="DATASOURCE_SOURCE_URL")
    result = executor.profile_relationship(executor.ProfileInput(datasource=source,
        source_table="public.events", source_columns=["entity_ref"], target_table="public.entities", target_columns=["id"],
        sample_rows=100, allow_bounded_unindexed=True, target_unique_index_confirmed=True))
    assert result["source_nonnull_distinct_keys"] == 12
    assert result["directional_coverage"] == 1
    assert result["source_exhausted"]
    assert result['reverse']['directional_coverage'] == 1
    assert len(calls) == 4


def test_indexed_sampling_and_target_membership_use_distinct_keys(monkeypatch):
    calls = []
    def query(body, token=None):
        calls.append(body.sql)
        assert 'SELECT DISTINCT' in body.sql or 'GROUP BY' in body.sql
        return {'rows': [[1], [2]]}
    monkeypatch.setattr(executor, 'execute_query', query)
    source = executor.DataSourceInput(datasource_id='source', type='mysql', secret_ref='DATASOURCE_SOURCE_URL')
    result = executor.profile_relationship(executor.ProfileInput(datasource=source,
        source_table='events', source_columns=['entity_ref'], target_table='entities', target_columns=['id'],
        source_index_confirmed=True, target_unique_index_confirmed=True, sample_rows=100))
    assert result['source_exhausted'] and result['matched_keys'] == 2
    assert result['sampling_unit'] == 'distinct_keys'
    assert result['reverse']['sampling_method'] == 'database_random_distinct_keys'
    assert len(calls) == 4


def test_optional_enum_context_exposes_only_declared_codes_not_arbitrary_values(monkeypatch):
    def query(body, token=None):
        if 'CASE' in body.sql:
            assert 'secret' not in body.sql
            assert 'GROUP BY' in body.sql and 'LIMIT 3' in body.sql
            return {'rows': [['2', 1], ['unlisted', 1]]}
        return {'rows': [[10], [20]]}
    monkeypatch.setattr(executor, 'execute_query', query)
    source = executor.DataSourceInput(datasource_id='source', type='mysql', secret_ref='DATASOURCE_SOURCE_URL')
    result = executor.profile_relationship(executor.ProfileInput(datasource=source,
        source_table='events', source_columns=['ref'], target_table='registry', target_columns=['id'],
        allow_bounded_unindexed=True, target_context_enums={'kind': [1, 2]}))
    assert result['target_context'] == {'kind': {'2': 1, 'unlisted': 1}}
    assert result['target_context_errors'] == []
