package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agent-substrate/substrate/internal/ateompath"
	"github.com/agent-substrate/substrate/internal/proto/ateletpb"
)

func TestPopotTerminateWithoutSandboxState(t *testing.T) {
	useTempNodeDirs(t)
	ctx := t.Context()
	const actorUID = "empty-actor-uid"
	req := &ateletpb.TerminateRequest{
		Atespace: "ate-demo", ActorName: "empty-actor", ActorUid: actorUID,
		TargetAteomUid: "ateom-uid-1",
	}
	herder := &AteomHerder{systemInfoVolumes: newSystemInfoVolumeRefresher(nil, nil)}
	for attempt := 0; attempt < 2; attempt++ {
		if _, err := herder.Terminate(ctx, req); err != nil {
			t.Fatalf("terminating actor without runtime state (attempt %d): %v", attempt, err)
		}
	}
	if release, err := popotBeginWorkloadOperation(ctx, ateompath.ActorPath(actorUID), false); err == nil {
		release()
		t.Fatal("successful deletion must still reject delayed Run/Restore")
	}
}

func TestPopotTerminateMissingRecordDoesNotIgnoreRuntimeState(t *testing.T) {
	useTempNodeDirs(t)
	const actorUID = "partial-actor-uid"
	dir := ateompath.ActorPath(actorUID)
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "runtime-state"), []byte("not cleaned"), 0600); err != nil {
		t.Fatal(err)
	}
	herder := &AteomHerder{}
	_, err := herder.Terminate(t.Context(), &ateletpb.TerminateRequest{
		Atespace: "ate-demo", ActorName: "partial-actor", ActorUid: actorUID,
		TargetAteomUid: "ateom-uid-1",
	})
	if err == nil || !strings.Contains(err.Error(), "failed to read sandbox record") {
		t.Fatalf("missing metadata with runtime state must remain an error: %v", err)
	}
}
