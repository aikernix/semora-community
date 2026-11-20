"""Internal role outputs, independent from LLM transport and execution."""

from dataclasses import dataclass
from semora_contracts import QueryPlan, RequirementDraft


@dataclass(frozen=True)
class AnalysisOutcome:
    plan: QueryPlan | None
    requirement: RequirementDraft | None = None
    model_mode: str = "sql-native"
