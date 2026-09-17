package agent

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHTTPAgentSendsUser(t *testing.T) {
	var got map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"ok"}}]}`))
	}))
	defer server.Close()

	agent := NewHTTPAgent(HTTPAgentConfig{Endpoint: server.URL, APIKey: "k", Model: "m"})
	if _, err := agent.Chat(context.Background(), "contact-123", "hi"); err != nil {
		t.Fatal(err)
	}
	if got["user"] != "contact-123" {
		t.Fatalf("expected user=contact-123, got %v", got["user"])
	}
}
