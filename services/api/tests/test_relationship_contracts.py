import json
import pytest
from pydantic import ValidationError
from app.relationship_contracts import DiscoveryOutput, prompt_schema
from app.relationship_output_schema import DISCOVERY_SCHEMA


def test_prompt_and_local_validation_share_one_contract():
    assert DISCOVERY_SCHEMA==prompt_schema(DiscoveryOutput)
    assert DiscoveryOutput.model_validate({'relations':[],'leads':[],'complete':True}).complete


@pytest.mark.parametrize('value',[{'relations':[],'complete':True},{'relations':[],'leads':{},'complete':True},{'relations':[],'leads':[],'complete':'true'}])
def test_missing_fields_and_wrong_json_types_are_rejected(value):
    with pytest.raises(ValidationError):DiscoveryOutput.model_validate(value)


def test_structure_does_not_impose_database_type_names_or_relation_count():
    relation={'source_table_id':'a','target_table_id':'b','key_pairs':[{'source':'x','target':'y'}]}
    result=DiscoveryOutput.model_validate({'relations':[relation]*30,'leads':[],'complete':True})
    assert len(result.relations)==30
    assert 'examples' not in json.dumps(DISCOVERY_SCHEMA)
