"""Small JSON output contracts for calls that do not use tool schemas."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class SQLExplanation(BaseModel):
    explanation: str = Field(min_length=1)


class AnswerExplanation(BaseModel):
    answer: str = Field(min_length=1)


class CandidateProposal(BaseModel):
    term: str = Field(min_length=1)
    proposal: str = Field(min_length=1)
    kind: Literal['definition','metric','object','property','relation']
    patch: dict[str,Any] = Field(description='definition为空；metric包含name、label、expression、grain、dimensions、format，引用当前对象属性；其他映射须原文明确支持。')
    evidence_chunk_id: str = Field(description='支撑候选的给定分块id。')


class CandidateBatch(BaseModel):
    candidates: list[CandidateProposal]
