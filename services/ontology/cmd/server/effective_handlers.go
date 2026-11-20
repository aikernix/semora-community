package main

import (
	"encoding/json"
	"errors"
	"net/http"
)

func (s *server) createDomain(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		jsonResponse(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	var input struct {
		Domain string `json:"domain"`
		Name   string `json:"name"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&input) != nil {
		jsonResponse(w, 422, map[string]string{"error": "业务域参数无效"})
		return
	}
	value, err := s.store.createDomain(r.Context(), input.Domain, input.Name)
	if errors.Is(err, errDomainExists) {
		jsonResponse(w, 409, map[string]string{"error": err.Error()})
		return
	}
	if validation, ok := err.(*validationError); ok {
		jsonResponse(w, 422, map[string]any{"error": validation.Error(), "issues": validation.issues})
		return
	}
	if err != nil {
		jsonResponse(w, 500, map[string]string{"error": "业务域创建失败"})
		return
	}
	jsonResponse(w, 201, value)
}

func (s *server) effectiveConfiguration(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodGet {
		s.draft(w, r)
		return
	}
	if r.Method != http.MethodPut {
		jsonResponse(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	var input Ontology
	if json.NewDecoder(http.MaxBytesReader(w, r.Body, 32<<20)).Decode(&input) != nil {
		jsonResponse(w, 422, map[string]string{"error": "本体配置不是有效JSON"})
		return
	}
	if input.Domain != r.PathValue("domain") {
		jsonResponse(w, 409, map[string]string{"error": "业务域标识不可更改"})
		return
	}
	value, err := s.store.saveEffective(r.Context(), input)
	if isNotFound(err) {
		jsonResponse(w, 404, map[string]string{"error": "业务域不存在"})
		return
	}
	if validation, ok := err.(*validationError); ok {
		jsonResponse(w, 422, map[string]any{"error": validation.Error(), "issues": validation.issues})
		return
	}
	if err != nil {
		jsonResponse(w, 500, map[string]string{"error": "本体保存失败"})
		return
	}
	jsonResponse(w, 200, value)
}
