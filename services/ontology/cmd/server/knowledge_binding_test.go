package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestKnowledgeBindingRejectsInvalidPayloadBeforeDatabaseAccess(t *testing.T) {
	for _, body := range []string{
		`{}`, `{"knowledgeBaseIds":null}`, `{"knowledgeBaseIds":[""]}`,
		`{"knowledgeBaseIds":[" kb"]}`, `{"knowledgeBaseIds":[],"objects":[]}`,
		`{"knowledgeBaseIds":"kb"}`,
	} {
		request := httptest.NewRequest(http.MethodPatch, "/", strings.NewReader(body))
		response := httptest.NewRecorder()
		(&server{}).knowledgeBinding(response, request)
		if response.Code != 422 {
			t.Fatalf("body %s: expected 422, got %d", body, response.Code)
		}
	}
	request := httptest.NewRequest(http.MethodPut, "/", strings.NewReader(`{"knowledgeBaseIds":[]}`))
	response := httptest.NewRecorder()
	(&server{}).knowledgeBinding(response, request)
	if response.Code != 405 {
		t.Fatalf("expected 405, got %d", response.Code)
	}
}
