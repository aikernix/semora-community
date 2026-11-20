import pytest
import sqlglot

from app.sql_column_refs import validate_physical_column_refs
from app.sql_errors import SQLIssue

OBJECTS = [{'table':'records','properties':[
    {'name':'key_ref','column':'key'}, {'name':'owner_ref','column':'owner'},
    {'name':'amount','column':'amount'},
]}]


@pytest.mark.parametrize('sql',[
    'SELECT r.owner_ref FROM records r',
    'SELECT owner_ref FROM records',
    'SELECT r.key FROM records r WHERE EXISTS (SELECT 1 FROM records x WHERE x.key = r.owner_ref)',
    'SELECT owner_ref AS owner_ref FROM records',
    'SELECT q.owner_ref FROM (SELECT r.owner_ref FROM records r) q',
])
def test_internal_id_misuse_reports_exact_physical_mapping(sql):
    with pytest.raises(SQLIssue, match='对应column为owner') as caught:
        validate_physical_column_refs(sqlglot.parse_one(sql,read='postgres'),OBJECTS)
    assert caught.value.stage=='column_mapping'


@pytest.mark.parametrize('sql',[
    'SELECT r.owner FROM records r',
    'SELECT r.owner AS owner_ref FROM records r ORDER BY owner_ref',
    'WITH q AS (SELECT owner AS owner_ref FROM records) SELECT q.owner_ref FROM q',
    'SELECT q.owner_ref FROM (SELECT owner AS owner_ref FROM records) q',
    'SELECT r.some_new_column FROM records r',
])
def test_physical_columns_derived_aliases_and_unmapped_names_are_not_rewritten(sql):
    tree=sqlglot.parse_one(sql,read='postgres');before=tree.sql()
    validate_physical_column_refs(tree,OBJECTS)
    assert tree.sql()==before


def test_real_physical_name_wins_over_another_property_id():
    validate_physical_column_refs(sqlglot.parse_one('SELECT r.owner_ref FROM records r'),
        OBJECTS+[{'table':'records','properties':[{'name':'other','column':'owner_ref'}]}])
