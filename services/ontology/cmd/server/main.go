package main

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strings"
)

type server struct {
	store *Store
	token string
}

func jsonResponse(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func (s *server) internal(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if os.Getenv("REQUIRE_INTERNAL_TOKEN") != "false" && r.Header.Get("X-Semora-Internal-Token") != s.token {
			jsonResponse(w, http.StatusUnauthorized, map[string]string{"error": "invalid internal token"})
			return
		}
		next(w, r)
	}
}

func domainQuery(r *http.Request) string {
	domain := strings.TrimSpace(r.URL.Query().Get("domain"))
	if domain == "" {
		return "retail_credit"
	}
	return domain
}

func (s *server) drafts(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		jsonResponse(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	value, err := s.store.listDrafts(r.Context())
	if err != nil {
		jsonResponse(w, 500, map[string]string{"error": err.Error()})
		return
	}
	jsonResponse(w, 200, value)
}

func (s *server) draft(w http.ResponseWriter, r *http.Request) {
	domain := r.PathValue("domain")
	if r.Method == http.MethodGet {
		value, err := s.store.getDraft(r.Context(), domain)
		if isNotFound(err) {
			jsonResponse(w, 404, map[string]string{"error": "draft not found"})
			return
		}
		if err != nil {
			jsonResponse(w, 500, map[string]string{"error": err.Error()})
			return
		}
		jsonResponse(w, 200, value)
		return
	}
	if r.Method == http.MethodPut {
		var value Ontology
		if err := json.NewDecoder(r.Body).Decode(&value); err != nil {
			jsonResponse(w, 400, map[string]string{"error": "invalid ontology"})
			return
		}
		if value.Domain != domain {
			jsonResponse(w, 409, map[string]string{"error": "domain cannot be changed"})
			return
		}
		if err := s.store.saveDraft(r.Context(), value); err != nil {
			if validation, ok := err.(*validationError); ok {
				jsonResponse(w, 422, map[string]any{"error": validation.Error(), "issues": validation.issues})
				return
			}
			jsonResponse(w, 500, map[string]string{"error": err.Error()})
			return
		}
		jsonResponse(w, 200, value)
		return
	}
	jsonResponse(w, 405, map[string]string{"error": "method not allowed"})
}

func (s *server) publish(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		jsonResponse(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	var input struct {
		Domain string `json:"domain"`
	}
	_ = json.NewDecoder(r.Body).Decode(&input)
	if input.Domain == "" {
		input.Domain = "retail_credit"
	}
	value, err := s.store.publish(r.Context(), input.Domain)
	if validation, ok := err.(*validationError); ok {
		jsonResponse(w, 422, map[string]any{"error": validation.Error(), "issues": validation.issues})
		return
	}
	if isNotFound(err) {
		jsonResponse(w, 404, map[string]string{"error": "draft not found"})
		return
	}
	if err != nil {
		jsonResponse(w, 500, map[string]string{"error": err.Error()})
		return
	}
	jsonResponse(w, 201, value)
}

func (s *server) knowledgeBinding(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPatch {
		jsonResponse(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	var input struct {
		IDs *[]string `json:"knowledgeBaseIds"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&input) != nil || input.IDs == nil || len(*input.IDs) > 20 {
		jsonResponse(w, 422, map[string]string{"error": "请提供不超过20项的知识库ID数组"})
		return
	}
	ids := []string{}
	seen := map[string]bool{}
	for _, id := range *input.IDs {
		if strings.TrimSpace(id) == "" || len(id) > 200 || id != strings.TrimSpace(id) {
			jsonResponse(w, 422, map[string]string{"error": "知识库ID无效"})
			return
		}
		if !seen[id] {
			ids = append(ids, id)
			seen[id] = true
		}
	}
	domain := r.PathValue("domain")
	if err := s.store.bindEffectiveKnowledgeBases(r.Context(), domain, ids); err != nil {
		if validation, ok := err.(*validationError); ok {
			jsonResponse(w, 422, map[string]any{"error": validation.Error(), "issues": validation.issues})
			return
		}
		if isNotFound(err) {
			jsonResponse(w, 404, map[string]string{"error": "draft not found"})
			return
		}
		jsonResponse(w, 500, map[string]string{"error": "知识库绑定保存失败"})
		return
	}
	jsonResponse(w, 200, map[string]any{"domain": domain, "knowledgeBaseIds": ids})
}

func (s *server) latest(w http.ResponseWriter, r *http.Request) {
	value, err := s.store.latestSnapshot(r.Context(), domainQuery(r))
	if isNotFound(err) {
		jsonResponse(w, 404, map[string]string{"error": "snapshot not found"})
		return
	}
	if err != nil {
		jsonResponse(w, 500, map[string]string{"error": err.Error()})
		return
	}
	jsonResponse(w, 200, value)
}

func (s *server) paths(w http.ResponseWriter, r *http.Request) {
	from, to := r.URL.Query().Get("from"), r.URL.Query().Get("to")
	if from == "" || to == "" {
		jsonResponse(w, 400, map[string]string{"error": "from and to are required"})
		return
	}
	snapshot, err := s.store.latestSnapshot(r.Context(), domainQuery(r))
	if err != nil {
		jsonResponse(w, 404, map[string]string{"error": "snapshot not found"})
		return
	}
	objects, relations, found := findRelationshipPath(snapshot.Ontology, from, to)
	if found {
		jsonResponse(w, 200, map[string]any{"domain": snapshot.Domain, "version": snapshot.Version, "objects": objects, "relations": relations})
		return
	}
	jsonResponse(w, 404, map[string]string{"error": "relationship path not found"})
}

func (s *server) candidates(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodPost {
		var input CandidateCreate
		if json.NewDecoder(r.Body).Decode(&input) != nil {
			jsonResponse(w, 400, map[string]string{"error": "invalid candidate"})
			return
		}
		value, err := s.store.createCandidate(r.Context(), input)
		if err != nil {
			jsonResponse(w, 422, map[string]string{"error": err.Error()})
			return
		}
		jsonResponse(w, 201, value)
		return
	}
	if r.Method != http.MethodGet {
		jsonResponse(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	value, err := s.store.listCandidates(r.Context(), r.URL.Query().Get("domain"))
	if err != nil {
		jsonResponse(w, 500, map[string]string{"error": err.Error()})
		return
	}
	jsonResponse(w, 200, value)
}

func (s *server) candidateDecision(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		jsonResponse(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	var input struct {
		Decision string `json:"decision"`
		Reviewer string `json:"reviewer"`
		Note     string `json:"note"`
	}
	if json.NewDecoder(r.Body).Decode(&input) != nil || (input.Decision != "accepted" && input.Decision != "rejected") {
		jsonResponse(w, 400, map[string]string{"error": "decision must be accepted or rejected"})
		return
	}
	value, err := s.store.decideCandidate(r.Context(), r.PathValue("id"), input.Decision, input.Reviewer, input.Note)
	if isNotFound(err) {
		jsonResponse(w, 404, map[string]string{"error": "candidate not found"})
		return
	} else if validation, ok := err.(*validationError); ok {
		jsonResponse(w, 422, map[string]any{"error": validation.Error(), "issues": validation.issues})
		return
	} else if err != nil {
		jsonResponse(w, 409, map[string]string{"error": err.Error()})
		return
	}
	jsonResponse(w, 200, value)
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		response, err := http.Get("http://localhost:8081/healthz")
		if err != nil || response.StatusCode != http.StatusOK {
			os.Exit(1)
		}
		_ = response.Body.Close()
		return
	}
	dsn := os.Getenv("ONTOLOGY_DATABASE_URL")
	if dsn == "" {
		dsn = "postgresql://semora:semora@localhost:5432/semora_business"
	}
	if len(os.Args) > 1 && os.Args[1] == "migrate" {
		seedDemo := strings.EqualFold(os.Getenv("SEMORA_DEMO_SEED"), "true")
		if err := migrateStore(context.Background(), dsn, seedDemo); err != nil {
			log.Fatal(err)
		}
		log.Printf("ontology schema migrated (demo seed: %t)", seedDemo)
		return
	}
	store, err := newStore(dsn)
	if err != nil {
		log.Fatal(err)
	}
	defer store.db.Close()
	app := &server{store: store, token: os.Getenv("INTERNAL_TOKEN")}
	if app.token == "" {
		app.token = "semora-dev-internal"
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		if err := store.db.PingContext(context.Background()); err != nil {
			jsonResponse(w, 503, map[string]string{"status": "error"})
			return
		}
		jsonResponse(w, 200, map[string]any{
			"status":             "ok",
			"service":            "semora-ontology",
			"implementation":     "semora-native",
			"externalDependency": false,
			"capabilities": []string{
				"ontology-drafts",
				"candidate-review",
				"immutable-snapshots",
				"relationship-paths",
				"ontology-validation",
			},
		})
	})
	mux.HandleFunc("/api/v1/ontology/drafts", app.internal(app.drafts))
	mux.HandleFunc("/api/v1/ontology/drafts/{domain}", app.internal(app.draft))
	mux.HandleFunc("/api/v1/ontology/drafts/{domain}/knowledge-bases", app.internal(app.knowledgeBinding))
	mux.HandleFunc("/api/v1/ontology/configurations/{domain}", app.internal(app.effectiveConfiguration))
	mux.HandleFunc("/api/v1/ontology/configurations/{domain}/sources/{source}", app.internal(app.unbindSource))
	mux.HandleFunc("/api/v1/ontology/source-packages", app.internal(app.sourcePackage))
	mux.HandleFunc("/api/v1/ontology/cross-relations/append", app.internal(app.appendCrossRelations))
	mux.HandleFunc("/api/v1/ontology/domains", app.internal(app.createDomain))
	mux.HandleFunc("DELETE /api/v1/ontology/domains/{domain}", app.internal(func(w http.ResponseWriter, r *http.Request) {
		deleteResource(w, r, app.store.deleteDomain, "domain")
	}))
	mux.HandleFunc("DELETE /internal/v1/datasources/{source}", app.internal(func(w http.ResponseWriter, r *http.Request) {
		deleteResource(w, r, app.store.deleteDatasource, "source")
	}))
	mux.HandleFunc("/api/v1/ontology/publish", app.internal(app.publish))
	mux.HandleFunc("/api/v1/ontology/snapshots/latest", app.internal(app.latest))
	mux.HandleFunc("/api/v1/ontology/paths", app.internal(app.paths))
	mux.HandleFunc("/api/v1/ontology/candidates", app.internal(app.candidates))
	mux.HandleFunc("/api/v1/ontology/candidates/{id}/decision", app.internal(app.candidateDecision))
	log.Println("ontology core listening on :8081")
	log.Fatal(http.ListenAndServe(":8081", mux))
}
