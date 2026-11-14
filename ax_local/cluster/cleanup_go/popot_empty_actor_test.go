package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestPopotWorkloadHasNoStateRequiresOnlyRegularLifecycleFiles(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"popot-lifecycle.lock", popotTerminalFence} {
		if empty, err := popotWorkloadHasNoState(dir); err != nil || empty {
			t.Fatalf("incomplete lifecycle files must not prove cleanup: %v %v", empty, err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), nil, 0600); err != nil {
			t.Fatal(err)
		}
	}
	if empty, err := popotWorkloadHasNoState(dir); err != nil || !empty {
		t.Fatalf("only lifecycle files remain: %v %v", empty, err)
	}
	state := filepath.Join(dir, "runtime-state")
	if err := os.WriteFile(state, nil, 0600); err != nil {
		t.Fatal(err)
	}
	if empty, err := popotWorkloadHasNoState(dir); err != nil || empty {
		t.Fatalf("runtime state must prevent cleanup acknowledgement: %v %v", empty, err)
	}
	if err := os.Remove(state); err != nil {
		t.Fatal(err)
	}
	fence := filepath.Join(dir, popotTerminalFence)
	if err := os.Remove(fence); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(state, fence); err != nil {
		t.Fatal(err)
	}
	if empty, err := popotWorkloadHasNoState(dir); err != nil || empty {
		t.Fatalf("symlink must not stand in for a lifecycle file: %v %v", empty, err)
	}
	link := filepath.Join(t.TempDir(), "actor")
	if err := os.Symlink(dir, link); err != nil {
		t.Fatal(err)
	}
	if empty, err := popotWorkloadHasNoState(link); err == nil || empty {
		t.Fatalf("symlink actor directory must be rejected: %v %v", empty, err)
	}
}
