package substrate

import (
	"reflect"
	"strings"
	"testing"

	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
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

func TestPopotFullSnapshots(t *testing.T) {
	image := "localhost:5001/popot-agent-ax@sha256:" + strings.Repeat("a", 64)
	for _, tt := range []struct {
		name  string
		image string
		mode  string
		want  ateapipb.SnapshotContentScope
	}{
		{"session", image, "full", ateapipb.SnapshotContentScope_SNAPSHOT_CONTENT_SCOPE_FULL},
		{"no opt in", image, "", ateapipb.SnapshotContentScope_SNAPSHOT_CONTENT_SCOPE_DATA},
		{"other image", "unrelated:latest", "full", ateapipb.SnapshotContentScope_SNAPSHOT_CONTENT_SCOPE_DATA},
	} {
		t.Run(tt.name, func(t *testing.T) {
			template := BuildActorTemplate("ate-demo-counter", "test", tt.image,
				map[string]string{"POPOT_SESSION_SNAPSHOT": tt.mode}, nil, "gs://snapshots/test")
			if template.SnapshotsConfig.GetOnPause() != tt.want || template.SnapshotsConfig.GetOnCommit() != tt.want {
				t.Fatalf("snapshot scopes = %v, want %v", template.SnapshotsConfig, tt.want)
			}
		})
	}
}
