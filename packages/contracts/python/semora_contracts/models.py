from __future__ import annotations
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .query_limits import EXPLAIN_TIMEOUT_DEFAULT_SECONDS, QUERY_TIMEOUT_DEFAULT_SECONDS, RESULT_DEFAULT_MIB, RESULT_DEFAULT_ROWS


def utc_now():
    return datetime.now(timezone.utc)


class RunStatus(StrEnum):
    RECEIVED = "received"
    RETRIEVING = "retrieving"
    UNDERSTANDING = "understanding"
    ALIGNING = "aligning"
    CLARIFYING = "clarifying"
    ALIGNED = "aligned"
    PLANNING = "planning"
    COMPILING = "compiling"
    GATING = "gating"
    REVIEWING = "reviewing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    EXPLAINING = "explaining"
    COMPLETED = "completed"
    FAILED = "failed"


class EvidenceRef(BaseModel):
    knowledge_base_id: str
    document_id: str
    chunk_id: str
    locator: str
    revision: str
    title: str
    summary: str


class KnowledgeResearchStep(BaseModel):
    kind: str
    tool: str = ""
    summary: str
    status: str = "completed"


class KnowledgeResearchResult(BaseModel):
    provider: str
    agent_id: str
    session_id: str
    answer: str = ""
    steps: list[KnowledgeResearchStep] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class OntologySnapshotRef(BaseModel):
    domain: str
    version: str
    content_hash: str
    artifact_version: str
    data_source_id: str = ""
    knowledge_base_ids: list[str] = Field(default_factory=list)


class OntologyCandidateEvidence(BaseModel):
    knowledge_base_id: str = ""
    document_id: str = ""
    chunk_id: str = ""
    locator: str = ""
    revision: str = ""
    excerpt: str = ""


class OntologyCandidate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    domain: str
    term: str
    source: str
    proposal: str
    kind: Literal["definition", "metric", "object", "property", "relation"] = "definition"
    patch: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending_review", "accepted", "rejected"] = "pending_review"
    evidence: OntologyCandidateEvidence | dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(alias="createdAt")
    reviewed_at: datetime | None = Field(default=None, alias="reviewedAt")
    reviewed_by: str = Field(default="", alias="reviewedBy")
    review_note: str = Field(default="", alias="reviewNote")


class CandidateExtractionResult(BaseModel):
    provider: str = "semora-native"
    extractor_mode: Literal["deterministic", "llm"]
    domain: str
    document_id: str
    count: int
    candidates: list[OntologyCandidate]


class ClarificationOption(BaseModel):
    id: str
    label: str = Field(description="选项的日常业务说法，不含表列名、英文编码、SQL表达式或技术实现。")
    value: str = Field(description="内部选项值，可包含技术标识；仅用于提交与分支判断，不显示给用户。")
    description: str = Field(default="", description="用自然语言说明此选择的业务含义，不展示字段名、编码或计算表达式。")


class QuestionCondition(BaseModel):
    question_id: str
    values: list[str] = Field(min_length=1)


class ClarificationQuestion(BaseModel):
    id: str
    field: str
    question: str = Field(description="用大白话询问业务含义，不出现表名、字段名、SQL、状态编码或技术实现选项。")
    reason: str = Field(description="说明选择会怎样改变统计结果，不引用数据库字段或表达式。")
    options: list[ClarificationOption] = Field(
        default_factory=list, description="可选业务解释，同时允许用户自由补充。"
    )
    allow_free_text: bool = True
    blocking: bool = True
    depends_on: list[QuestionCondition] = Field(
        default_factory=list,
        description="全部条件满足才展示并要求回答；values内任一值可匹配父问题的选项value。",
    )

    @field_validator("depends_on", mode="before")
    @classmethod
    def normalize_unused_conditions(cls, value):
        return [] if value is None else value


class QueryConfigurationGap(BaseModel):
    kind: Literal["relation", "field", "definition", "capability"] = Field(
        description="技术障碍类型：relation为关联或键映射缺失，field为必需数据/物理映射缺失，capability为可核验的执行能力缺口。definition仅兼容历史记录；业务含义、范围及统计口径待确认请放questions。"
    )
    summary: str = Field(min_length=1, max_length=1000)
    required_for: Literal["output", "filter", "time", "join", "aggregation", "sort"]
    objects: list[str] = Field(
        default_factory=list, max_length=8, description="已有对象的准确name，不使用中文label。"
    )
    field_refs: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="已有且可核验的对象.属性引用。不存在的字段只在summary描述所需含义，不在此猜测字段名。",
    )
    evidence_refs: list[str] = Field(
        min_length=1,
        max_length=8,
        description="依据标识：用户需求用request，本体或表结构用ontology，也可引用给定对象name、字段引用或知识ref。不要自造资料ID。",
    )
    resolution: str = Field(min_length=1, max_length=1000)
    depends_on: list[QuestionCondition] = Field(default_factory=list)
    capability_key: str | None = Field(
        default=None, description="能力缺口须引用planning_capabilities.unsupported中的真实键。"
    )


class QueryConfidence(BaseModel):
    level: Literal["high", "medium", "low"]
    reason: str = Field(
        min_length=1,
        max_length=240,
        description="用业务语言简述资料依据或不确定性，不出现表列标识或SQL；是模型自评，不是正确概率。",
    )


class BusinessRequirements(BaseModel):
    """Business meaning only; this is not a SQL execution plan."""
    subject: str = ""
    time_semantics: str | None = None
    grain: str | None = None
    data_scope: dict[str, Any] = Field(default_factory=dict)
    output_fields: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    aggregation_rules: list[str] = Field(default_factory=list)
    ordering: list[str] = Field(default_factory=list)
    sequence: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("output_fields", "metrics", "dimensions", "filters", "aggregation_rules",
                     "ordering", "sequence", "assumptions", mode="before")
    @classmethod
    def normalize_lists(cls, value):
        return [] if value is None else value

    @field_validator("data_scope", mode="before")
    @classmethod
    def normalize_scope(cls, value):
        return {} if value is None else value

    @field_validator("subject", mode="before")
    @classmethod
    def normalize_subject(cls, value):
        return "" if value is None else value


class LogicalMetric(BaseModel):
    """A business calculation bound to published semantic references."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=64)
    label: str = Field(min_length=1, max_length=200)
    operation: Literal["value", "count", "count_distinct", "sum", "avg", "min", "max", "derived"]
    field_refs: list[str] = Field(default_factory=list, max_length=8)


class LogicalFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_refs: list[str] = Field(default_factory=list, max_length=8)
    relation_ref: str | None = None
    operator: Literal[
        "eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in", "between",
        "like", "is_null", "is_not_null", "exists", "not_exists", "null_safe_eq", "null_safe_neq",
    ]
    value: Any = None
    description: str = Field(min_length=1, max_length=300)


class LogicalSort(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_ref: str = Field(min_length=1, max_length=200, description="指标id或对象.属性引用")
    direction: Literal["asc", "desc"] = "asc"


class LogicalQueryPlan(BaseModel):
    """Internal semantic contract; labels explain it, stable refs drive validation."""

    model_config = ConfigDict(extra="forbid")
    subject_refs: list[str] = Field(default_factory=list, max_length=8)
    output_refs: list[str] = Field(default_factory=list, max_length=32)
    grain_refs: list[str] = Field(default_factory=list, description="每行结果的身份或分组字段引用；使用对象.属性，复合粒度保留全部字段。")
    dimension_refs: list[str] = Field(default_factory=list, max_length=16)
    metrics: list[LogicalMetric] = Field(default_factory=list, max_length=16)
    filters: list[LogicalFilter] = Field(default_factory=list, max_length=24)
    join_filters: list[LogicalFilter] = Field(default_factory=list, max_length=24,
        description="本次查询要求的完整关联比较与附加限定；两字段比较时field_refs按左右顺序填写，常量条件用单字段与value；不得只引用已有关系而遗漏用户确认的其他匹配条件。")
    relation_refs: list[str] = Field(default_factory=list, max_length=16)
    sort: list[LogicalSort] = Field(default_factory=list, max_length=8)
    limit: int | None = Field(default=None, ge=1, le=100000)
    sequence: list[str] = Field(default_factory=list, max_length=12)


class QueryObjectUse(BaseModel):
    object: str = Field(min_length=1, description="当前查询必需对象的准确name；不是检索候选列表。")
    requirement: str = Field(min_length=1, max_length=500, description="该对象承担的当前输出、筛选、统计、时间或连接路径要求；不能使用未来可能新增的需求。")
    reason: str = Field(min_length=1, max_length=500, description="结合已有定义说明该对象为何不可省略；关键词相似或出现在资料中不构成必要性依据。")


class QueryUnderstanding(BaseModel):
    summary: str = Field(min_length=1, max_length=1200, description="给业务用户看的需求理解：目标、范围及统计方式，不含数据库表列名、技术编码或SQL表达式。")
    target_state: Literal["known", "needs_direction"]
    confidence: QueryConfidence | None = None
    requirements: BusinessRequirements = Field(
        default_factory=BusinessRequirements,
        description="用业务语言记录目标、时间、范围、指标、分组、条件、每组含义、排序和先后顺序；只整理有依据的要求，不新增条件，不放SQL或执行步骤契约。",
    )

    @field_validator("requirements", mode="before")
    @classmethod
    def normalize_requirements(cls, value):
        return {} if value is None else value

    skeleton: list[str] = Field(
        default_factory=list,
        max_length=12,
        description="面向业务用户的步骤说明：查哪些对象、按什么范围与方式统计、有哪些待确认内容；用业务词语，不放表列标识、SQL或隐藏思维链。",
    )
    questions: list[ClarificationQuestion] = Field(
        default_factory=list,
        max_length=16,
        description="现有资料无法唯一确定且会改变结果的业务选择。提供业务语言选项与自由补充；同义表达可直接理解，不重复询问已有确认。",
    )
    required_object_uses: list[QueryObjectUse] = Field(
        default_factory=list,
        max_length=64,
        description="先判断单对象能否满足当前需求，再列最小必要对象及各自用途。关联缺口涉及的对象必须在此有必要性依据；待确认分支只列该分支确实需要的对象。历史记录可为空。",
    )
    technical_gaps: list[QueryConfigurationGap] = Field(
        default_factory=list,
        max_length=64,
        description="当前查询必需且有依据的关联、数据映射或执行障碍；候选对象间缺少关系、未来可能需要的联查及业务口径待确认均不属于当前配置缺口。",
    )
    logical_plan: LogicalQueryPlan | None = Field(
        default=None,
        description="资料与口径足够时填写的结构化查询要求；引用资料中的稳定对象、属性和关系标识。",
    )

    @field_validator("skeleton", "questions", "technical_gaps", mode="before")
    @classmethod
    def normalize_unused_collections(cls, value):
        return [] if value is None else value


class RequirementDraft(BaseModel):
    model_config = ConfigDict(extra="allow")
    clarification_stage: Literal["direction", "business"] = "business"
    clarification_round: int = Field(default=0, ge=0, le=2)
    version: int = 1
    summary: str
    analysis_steps: list[str] = Field(default_factory=list)
    subject: str = ""
    time_semantics: str | None = None
    grain: str | None = None
    data_scope: dict[str, Any] = Field(default_factory=dict)
    output_fields: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: dict[str, float] = Field(default_factory=dict)
    clarifications: list[ClarificationQuestion] = Field(default_factory=list)
    technical_gaps: list[QueryConfigurationGap] = Field(default_factory=list)
    status: Literal["draft", "awaiting_clarification", "ready"] = "draft"
    logical_plan: LogicalQueryPlan | None = None


class ConfirmedFact(BaseModel):
    question_id: str
    field: str
    value: str
    label: str


class RequirementSpec(BaseModel):
    """Confirmed business meaning; legacy fields are read-only historic data."""

    model_config = ConfigDict(extra="allow")
    version: str
    content_hash: str
    source_draft_version: int
    summary: str
    analysis_steps: list[str] = Field(default_factory=list)
    subject: str = ""
    time_semantics: str | None = None
    grain: str | None = None
    data_scope: dict[str, Any] = Field(default_factory=dict)
    output_fields: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confirmed_facts: list[ConfirmedFact] = Field(default_factory=list)
    confirmed_by: str
    confirmed_at: datetime = Field(default_factory=utc_now)
    logical_plan: LogicalQueryPlan | None = None


class SQLInputBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_step: str
    source_columns: list[str] = Field(min_length=1, max_length=8)
    parameter: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    null_safe_columns: list[str] = Field(default_factory=list, max_length=8,
        description="source_columns中按需求或已确认条件允许空值与空值匹配的来源输出列；默认空值不匹配。不得仅因字段可空就启用。")

    @model_validator(mode="after")
    def validate_null_columns(self):
        if not set(self.null_safe_columns) <= set(self.source_columns):
            raise ValueError("null_safe_columns必须属于source_columns")
        return self


class SQLProgramStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=64)
    purpose: str = Field(min_length=1, max_length=500, description="用日常业务语言说明本步骤要取得什么信息，不含表列名、SQL片段或执行引擎术语。")
    engine: Literal["source", "local"] = Field(
        default="source",
        description="source查询物理数据源；local仅查询先前步骤的结果表，不能读取本体对象或物理源表。",
    )
    data_source_id: str = ""
    dialect: str = Field(default="", description="本步骤SQL方言，由程序根据执行目标确认。")
    sql: str = Field(
        min_length=1,
        max_length=100000,
        description="使用本步骤执行目标方言的完整SQL。标量参数:name；键集条件用SEMORA_KEYS_MATCH(:name,目标列...)绑定占位，由程序按目标方言展开；目标列顺序与inputs.source_columns一致。local仅读前序结果表。",
    )
    parameters: dict[str, Any] = Field(default_factory=dict)
    inputs: list[SQLInputBinding] = Field(
        default_factory=list,
        max_length=4,
        description="仅source步骤使用：将前置输出完整键元组绑定到本步键集占位；按已确定的比较语义声明null_safe_columns。local步骤必须为空，直接读取前序结果表。",
    )
    output_columns: list[str] = Field(min_length=1, max_length=1000)
    result_grain: list[str] = Field(default_factory=list, description="结果粒度对应的输出列，复合粒度保留全部列；标注粒度不能替代SQL聚合。")
    metric_sources: dict[str, list[str]] = Field(default_factory=dict, description="聚合输出列到来源表别名.字段的映射；每个指标独立说明来源。")
    output_labels: dict[str, str] = Field(default_factory=dict, description="业务界面的列标题：为每个output_columns提供简洁自然语言名称，key保持原列名，value不含字段编码或SQL表达式。")

    @field_validator("parameters", "inputs", "output_labels", mode="before")
    @classmethod
    def unused_values(cls, value, info):
        if info.field_name == "output_labels":
            return {k:v for k,v in value.items() if isinstance(k,str) and isinstance(v,str)} if isinstance(value,dict) else {}
        return ({} if info.field_name == "parameters" else []) if value is None else value


class SQLProgram(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1200, description="直接用于业务用户的结果说明：描述最终查询目标、时间范围、筛选与统计方式。不要写表列名、SQL表达式、技术实现、错误诊断或修复历史；尚未执行，不编造数据结论。")
    steps: list[SQLProgramStep] = Field(min_length=1, max_length=8)
    assumptions: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("assumptions", mode="before")
    @classmethod
    def unused_assumptions(cls, value):
        return [] if value is None else value

    @model_validator(mode="after")
    def ordered_dependencies(self):
        previous = {}
        for step in self.steps:
            if step.name in previous or len(set(step.output_columns)) != len(step.output_columns):
                raise ValueError("步骤名称和每步输出列名称必须唯一")
            # Display-only metadata must never invalidate executable SQL.
            step.output_labels = {k:v.strip() for k,v in step.output_labels.items()
                                  if k in step.output_columns and v.strip()}
            if step.engine == "source" and not step.data_source_id:
                raise ValueError("源查询必须指定data_source_id")
            if step.engine == "local" and (step.data_source_id or step.inputs):
                raise ValueError(
                    f"步骤{step.name}为local，不设置data_source_id或inputs；只能读取先前步骤结果表。"
                    "若需要其他本体对象的数据，必须先新增source查询，再用其步骤name在local关联。不能直接JOIN本体对象名或物理源表。"
                )
            for binding in step.inputs:
                if binding.source_step not in previous:
                    raise ValueError("输入必须引用先前步骤")
                if not set(binding.source_columns) <= set(
                    previous[binding.source_step].output_columns
                ):
                    raise ValueError("关联键必须在前置步骤输出列中")
            previous[step.name] = step
        return self


class QueryPlan(BaseModel):
    """SQL execution envelope. Extra keys only preserve historic plan displays."""

    model_config = ConfigDict(extra="allow")
    version: str = "sql-v1"
    domain: str
    intent: str
    sql_program: SQLProgram | None = None
    execution_mode: Literal["single_query", "sequential_queries"] = "single_query"
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    logical_plan: LogicalQueryPlan | None = None


class SQLArtifact(BaseModel):
    semantic_sql: str
    physical_sql: str
    dialect: str = "postgres"
    data_source_id: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    sensitive_columns: list[str] = Field(default_factory=list)
    requirement_version: str = ""
    requirement_hash: str = ""
    artifact_hash: str
    compilation: dict[str, Any] = Field(default_factory=dict)


class SystemSettings(BaseModel):
    """Bounded query runtime settings; no retired approval/reviewer switches."""

    query_timeout_seconds: int = Field(default=QUERY_TIMEOUT_DEFAULT_SECONDS, ge=1)
    explain_timeout_seconds: int = Field(default=EXPLAIN_TIMEOUT_DEFAULT_SECONDS, ge=1)
    max_result_rows: int = Field(default=RESULT_DEFAULT_ROWS, ge=1)
    max_result_mb: int = Field(default=RESULT_DEFAULT_MIB, ge=1)
    preview_rows: int = Field(default=100, ge=1, le=1000)
    version: int = Field(default=1, ge=1)
    policy_version: str = ""
    updated_at: datetime = Field(default_factory=utc_now)
    updated_by: str = "system"


class AuthorizationContext(BaseModel):
    """Immutable authorization snapshot attached to a query run."""

    subject: str
    username: str
    roles: list[str] = Field(default_factory=list)
    branch_ids: list[int] = Field(default_factory=list)
    sensitivity_clearance: Literal["internal", "confidential", "restricted"] = "internal"
    allowed_data_source_ids: list[str] | None = None
    cross_organization: bool = False
    policy_version: str = "abac-v1"


class ExecutionResult(BaseModel):
    column_types: list[str] = Field(default_factory=list)
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    duration_ms: int
    truncated: bool = False
    bytes_returned: int = 0
    summary: dict[str, Any] = Field(default_factory=dict)


class RunEvent(BaseModel):
    event: str
    status: RunStatus
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ExecutionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    step: str
    status: Literal["completed", "skipped"] = "completed"
    row_count: int = Field(ge=0)
    duration_ms: int = Field(default=0, ge=0)
    truncated: bool = False
    output_hash: str
    finished_at: datetime = Field(default_factory=utc_now)


class QueryDelivery(BaseModel):
    kind: Literal["sql", "result", "clarification", "blocked", "cancelled", "interrupted"]
    title: str
    summary: str
    warnings: list[str] = Field(default_factory=list)
    next_action: str = ""
    result_verified: bool = False


class QueryRun(BaseModel):
    # Historical payloads can carry former display metadata, never executable logic.
    model_config = ConfigDict(extra="allow")

    @field_validator("agent_memory", mode="before")
    @classmethod
    def restore_repair_budget(cls, value):
        if isinstance(value, dict) and isinstance(value.get("query_repair_budget"), dict):
            value = {**value, "query_repair_budget": [value["query_repair_budget"]]}
        return value

    clarification_history: list[dict[str, str]] = Field(default_factory=list)
    query_rehearsal: QueryUnderstanding | None = None
    delivery: QueryDelivery | None = None
    delivery_warnings: list[str] = Field(default_factory=list)
    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    domain: str = ""
    question: str
    message_mode: Literal["auto", "continue", "new"] = "auto"
    continuation: dict[str, Any] | None = None
    user_answers: list[ConfirmedFact] = Field(default_factory=list)
    status: RunStatus = RunStatus.RECEIVED
    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    authorization: AuthorizationContext | None = None
    events: list[RunEvent] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    knowledge_research: KnowledgeResearchResult | None = None
    ontology: OntologySnapshotRef | None = None
    requirement_draft: RequirementDraft | None = None
    requirement: RequirementSpec | None = None
    proposed_plan: QueryPlan | None = None
    alignment_rounds: list[dict[str, Any]] = Field(default_factory=list)
    agent_memory: dict[str, list[dict[str, Any]]] = Field(default_factory=dict, exclude=True)

    @field_validator("agent_memory", mode="before")
    @classmethod
    def migrate_sql_repair_counter(cls, value):
        # Compatibility with the first SQL-native preview's scalar counter.
        if isinstance(value, dict) and isinstance(value.get("sql_repair_count"), list):
            items = value["sql_repair_count"]
            if items and all(type(item) is int for item in items):
                return {**value, "sql_repair_count": [{"attempts": item} for item in items]}
        return value

    review_case: dict[str, Any] | None = Field(default=None, exclude=True)
    execution_binding: str = Field(default="", exclude=True)
    execution_receipts: dict[str, ExecutionReceipt] = Field(default_factory=dict, exclude=True)
    revision_count: int = 0
    plan: QueryPlan | None = None
    sql: SQLArtifact | None = None
    source_artifacts: dict[str, SQLArtifact] = Field(default_factory=dict)
    source_results: dict[str, ExecutionResult] = Field(default_factory=dict, exclude=True)
    source_reviews: dict[str, dict[str, Any]] = Field(default_factory=dict)
    sql_explanation: str | None = None
    gate: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    result_review: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    result: ExecutionResult | None = None
    answer: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


RequirementDraft.model_rebuild()
RequirementSpec.model_rebuild()
