package substrate

import (
	"reflect"
	"strings"
	"testing"
)

func TestPopotToolUser(t *testing.T) {
	image := "localhost:5001/popot-agent-ax@sha256:" + strings.Repeat("a", 64)
	for _, tt := range []struct {
		name  string
		image string
		uid   string
		want  []string
	}{
		{"tool role", image, "10001", []string{"SETUID", "SETGID"}},
		{"no tools", image, "", nil},
		{"invalid user", image, "0", nil},
		{"other image", "unrelated:latest", "10001", nil},
	} {
		t.Run(tt.name, func(t *testing.T) {
			template := BuildActorTemplate("ate-demo-counter", "test", tt.image,
				map[string]string{"POPOT_TOOLS_UID": tt.uid}, nil, "gs://snapshots/test")
			caps := template.Containers[0].GetSecurityContext().GetCapabilities()
			if !reflect.DeepEqual(caps.GetAdd(), tt.want) || len(caps.GetDrop()) != 0 {
				t.Fatalf("capabilities = %v, want only %v added", caps, tt.want)
			}
		})
	}
}
