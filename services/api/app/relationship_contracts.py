"""Provider-independent relation output contracts; only factual checks live elsewhere."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

ExclusionCode = Literal['', 'meaning_mismatch', 'replica', 'lineage_only', 'redundant_identity', 'transitive_only', 'wrong_target', 'shared_entity']


class Output(BaseModel):
    model_config=ConfigDict(strict=True, extra='ignore', allow_inf_nan=False)


class Guard(Output):
    side:Literal['source','target']
    field:str
    operator:Literal['eq']
    value:str|int|float|bool


class Pair(Output):
    source:str
    target:str


class Lead(Output):
    source_table_id:str
    fields:list[str]
    question:str
    keywords:list[str]


class Relation(Output):
    source_table_id:str
    target_table_id:str
    key_pairs:list[Pair]
    role:str=''
    reason:str=''
    guards:list[Guard]=Field(default_factory=list)


class DiscoveryOutput(Output):
    relations:list[Relation]
    leads:list[Lead]
    complete:StrictBool


class ReadOutput(Output):
    relations:list[dict]=Field(max_length=0)
    leads:list[Lead]
    complete:StrictBool


class EvidenceRequest(Output):
    candidate_id:str
    kind:Literal['definition','profile']


class Decision(Output):
    candidate_id:str
    supported:StrictBool
    structure_sufficient:StrictBool
    target_role:Literal['reference','identity','replica','unknown']
    alternative_unresolved:StrictBool
    reason:str=Field(min_length=1)
    query_outcome:Literal['usable','exclude','needs_evidence']
    exclusion_code:ExclusionCode=''
    missing_evidence:list[str]=Field(default_factory=list)
    query_role:str=''
    key_domain:Literal['same','different','unknown']
    relationship_kind:Literal['entity_reference','direct_record_reference','identity','lineage','shared_entity','unknown']
    coverage_expectation:Literal['complete','partial','unknown']
    coverage_assessment:Literal['supports_reference','incomplete_reference_data','identifier_conflict','unknown']='unknown'
    additional_predicates:list[Guard]


class ResearchOutput(Output):
    requests:list[EvidenceRequest]=Field(default_factory=list)
    decisions:list[Decision]


class KeywordOutput(Output):
    model_config=ConfigDict(strict=True, extra='forbid')
    keywords:list[str]


class OrganizedGroup(Output):
    table_ids:list[str]=Field(min_length=1)
    reason:str=Field(min_length=1)


class OrganizationOutput(Output):
    groups:list[OrganizedGroup]=Field(description='后续需要实际分析的全部组；每组必须列出完整成员，未列出的表不进入后续分析')


class ProposedRelation(Output):
    source_table_id:str
    target_table_id:str
    key_pairs:list[Pair]=Field(min_length=1)
    reason:str=Field(min_length=1)
    guards:list[Guard]=Field(default_factory=list)


class ProposalOutput(Output):
    relations:list[ProposedRelation]


class AgentConclusion(Output):
    relations:list[ProposedRelation]
    checked_table_ids:list[str]
    gaps:list[str]


class EvidenceGap(Output):
    reason:str=Field(min_length=1)
    request:Literal['definition','profile']|None=None


class CompactDecision(Output):
    candidate_id:str
    outcome:Literal['usable','exclude','needs_evidence']
    kind:Literal['entity_reference','direct_record_reference','identity','lineage','shared_entity','unknown']
    reason:str=Field(min_length=1)
    code:ExclusionCode=''
    gaps:list[EvidenceGap]=Field(default_factory=list)
    # Omitted means unchanged. [] explicitly removes existing additional conditions.
    predicates:list[Guard]|None=None

    @field_validator('gaps',mode='before')
    @classmethod
    def restore_legacy_gaps(cls,value):
        # Checkpoints may still contain the old list[str] wire format.
        return [{'reason':item} if isinstance(item,str) else item for item in (value or [])]


class DataCompactDecision(CompactDecision):
    coverage:Literal['supports_reference','incomplete_reference_data','identifier_conflict','unknown']='unknown'


class CompactResearchOutput(Output):
    decisions:list[CompactDecision]


class DataResearchOutput(Output):
    decisions:list[DataCompactDecision]


class AgentAction(Output):
    tool:Literal['catalog','read_tables','search','known_relations','profile','definition','submit','finish']
    arguments:dict


def prompt_schema(model):
    def clean(value):
        if isinstance(value,dict):return {k:clean(v) for k,v in value.items() if k not in {'default','examples','title'}}
        if isinstance(value,list):return [clean(v) for v in value]
        return value
    return clean(model.model_json_schema())
