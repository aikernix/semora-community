package main

import "time"

type Property struct {
	Description string         `json:"description,omitempty"`
	Metadata    map[string]any `json:"metadata,omitempty"`
	Name        string         `json:"name"`
	Label       string         `json:"label,omitempty"`
	Aliases     []string       `json:"aliases,omitempty"`
	Column      string         `json:"column"`
	Type        string         `json:"type"`
	Sensitive   bool           `json:"sensitive"`
	Nullable    bool           `json:"nullable,omitempty"`
}

type ObjectProjection struct {
	Distinct bool `json:"distinct"`
}

type ObjectClass struct {
	Projection   *ObjectProjection `json:"projection,omitempty"`
	DataSourceID string            `json:"dataSourceId,omitempty"`
	Metadata     map[string]any    `json:"metadata,omitempty"`
	Name         string            `json:"name"`
	Label        string            `json:"label"`
	Aliases      []string          `json:"aliases,omitempty"`
	Table        string            `json:"table"`
	PrimaryKey   string            `json:"primaryKey"`
	Properties   []Property        `json:"properties"`
}

type RelationClass struct {
	Metadata    map[string]any `json:"metadata,omitempty"`
	Name        string         `json:"name"`
	Label       string         `json:"label,omitempty"`
	From        string         `json:"from"`
	To          string         `json:"to"`
	JoinType    string         `json:"joinType"`
	Condition   string         `json:"condition"`
	Cardinality string         `json:"cardinality"`
	Optional    bool           `json:"optional,omitempty"`
}

type Metric struct {
	Name       string   `json:"name"`
	Label      string   `json:"label"`
	Aliases    []string `json:"aliases,omitempty"`
	Expression string   `json:"expression"`
	Grain      string   `json:"grain"`
	Dimensions []string `json:"dimensions,omitempty"`
	Format     string   `json:"format,omitempty"`
}

type DefaultFilter struct {
	Field    string `json:"field"`
	Operator string `json:"operator"`
	Value    any    `json:"value"`
}

type SourcePackageRef struct {
	DataSourceID string `json:"dataSourceId"`
	SchemaHash   string `json:"schemaHash"`
	Version      string `json:"version,omitempty"`
}

type Ontology struct {
	SourceScopes      map[string]string  `json:"sourceScopes,omitempty"`
	Metadata          map[string]any     `json:"metadata,omitempty"`
	Domain            string             `json:"domain"`
	Name              string             `json:"name"`
	Version           string             `json:"version"`
	Description       string             `json:"description,omitempty"`
	ScopeField        string             `json:"scopeField,omitempty"`
	TimeField         string             `json:"timeField,omitempty"`
	DataSourceID      string             `json:"dataSourceId"`
	KnowledgeBaseIDs  []string           `json:"knowledgeBaseIds"`
	Objects           []ObjectClass      `json:"objects"`
	Relations         []RelationClass    `json:"relations"`
	Metrics           []Metric           `json:"metrics"`
	DefaultFilters    []DefaultFilter    `json:"defaultFilters,omitempty"`
	SourcePackageRefs []SourcePackageRef `json:"sourcePackageRefs,omitempty"`
}

type Snapshot struct {
	ID              string         `json:"id"`
	Ontology        Ontology       `json:"ontology"`
	Domain          string         `json:"domain"`
	Version         string         `json:"version"`
	ContentHash     string         `json:"contentHash"`
	ArtifactVersion string         `json:"artifactVersion"`
	PublishedAt     time.Time      `json:"publishedAt"`
	WrenArtifact    map[string]any `json:"wrenArtifact,omitempty"`
}

type DraftSummary struct {
	Domain    string    `json:"domain"`
	Name      string    `json:"name"`
	Version   string    `json:"version"`
	Status    string    `json:"status"`
	Objects   int       `json:"objects"`
	Relations int       `json:"relations"`
	Metrics   int       `json:"metrics"`
	UpdatedAt time.Time `json:"updatedAt"`
}

type Candidate struct {
	ID         string         `json:"id"`
	Domain     string         `json:"domain"`
	Term       string         `json:"term"`
	Source     string         `json:"source"`
	Proposal   string         `json:"proposal"`
	Kind       string         `json:"kind"`
	Patch      map[string]any `json:"patch"`
	Status     string         `json:"status"`
	Evidence   any            `json:"evidence,omitempty"`
	CreatedAt  time.Time      `json:"createdAt"`
	ReviewedAt *time.Time     `json:"reviewedAt,omitempty"`
	ReviewedBy string         `json:"reviewedBy,omitempty"`
	ReviewNote string         `json:"reviewNote,omitempty"`
}

type CandidateCreate struct {
	Domain   string         `json:"domain"`
	Term     string         `json:"term"`
	Source   string         `json:"source"`
	Proposal string         `json:"proposal"`
	Kind     string         `json:"kind"`
	Patch    map[string]any `json:"patch"`
	Evidence any            `json:"evidence,omitempty"`
}

type ValidationIssue struct {
	Path    string `json:"path"`
	Message string `json:"message"`
}

func demoOntology() Ontology {
	return Ontology{
		Domain: "retail_credit", Name: "零售信贷本体", Version: "1.0.0", Description: "零售信贷客户、贷款、机构和还款表现语义模型",
		ScopeField: "branch.id", TimeField: "loan.as_of_date", DataSourceID: "retail-credit-pg",
		KnowledgeBaseIDs: []string{"11111111-1111-4111-8111-111111111111"},
		Objects: []ObjectClass{
			{Name: "branch", Label: "分行", Aliases: []string{"机构", "网点"}, Table: "branches", PrimaryKey: "id", Properties: []Property{{Name: "name", Label: "分行名称", Aliases: []string{"机构名称", "网点名称"}, Column: "name", Type: "string"}}},
			{Name: "customer", Label: "客户", Table: "customers", PrimaryKey: "id", Properties: []Property{{Name: "customer_no", Label: "客户号", Column: "customer_no", Type: "string"}, {Name: "name", Label: "姓名", Column: "name", Type: "string", Sensitive: true}, {Name: "mobile", Label: "手机号", Column: "mobile", Type: "string", Sensitive: true}, {Name: "id_no", Label: "证件号", Column: "id_no", Type: "string", Sensitive: true}}},
			{Name: "loan", Label: "贷款", Table: "loans", PrimaryKey: "id", Properties: []Property{{Name: "loan_no", Label: "贷款编号", Column: "loan_no", Type: "string"}, {Name: "outstanding_balance", Label: "贷款余额", Column: "outstanding_balance", Type: "decimal"}, {Name: "status", Label: "状态", Column: "status", Type: "string"}, {Name: "as_of_date", Label: "统计日期", Column: "as_of_date", Type: "date"}}},
			{Name: "payment", Label: "还款表现", Table: "payments", PrimaryKey: "id", Properties: []Property{{Name: "days_overdue", Label: "逾期天数", Column: "days_overdue", Type: "integer"}, {Name: "overdue_amount", Label: "逾期金额", Column: "overdue_amount", Type: "decimal"}}},
		},
		Relations: []RelationClass{
			{Name: "customer_has_loan", Label: "客户持有贷款", From: "customer", To: "loan", JoinType: "one_to_many", Cardinality: "1:N", Condition: "customer.id = loan.customer_id"},
			{Name: "loan_belongs_to_branch", Label: "贷款归属分行", From: "loan", To: "branch", JoinType: "many_to_one", Cardinality: "N:1", Condition: "loan.branch_id = branch.id"},
			{Name: "loan_has_payment", Label: "贷款具有还款表现", From: "loan", To: "payment", JoinType: "one_to_many", Cardinality: "1:N", Condition: "loan.id = payment.loan_id", Optional: true},
		},
		Metrics: []Metric{
			{Name: "loan_balance", Label: "贷款余额", Expression: "SUM(loan.outstanding_balance)", Grain: "branch", Dimensions: []string{"branch"}, Format: "currency"},
			{Name: "overdue_90_rate", Label: "90+逾期率", Aliases: []string{"90天逾期率", "90日以上逾期率"}, Expression: "SUM(payment.overdue_amount WHERE payment.days_overdue >= 90) / SUM(loan.outstanding_balance)", Grain: "branch", Dimensions: []string{"branch"}, Format: "percentage"},
		},
		DefaultFilters: []DefaultFilter{{Field: "loan.status", Operator: "eq", Value: "active"}},
	}
}
