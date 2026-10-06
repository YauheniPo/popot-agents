package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestWorkloadResetRequiresBothExistingEmptyDirectories(t *testing.T) {
	root := t.TempDir()
	bundle, state := filepath.Join(root, "bundle"), filepath.Join(root, "state")
	if reset, err := popotWorkloadWasReset(bundle, state); err == nil || reset {
		t.Fatal("missing directories must not bypass workload termination")
	}
	for _, dir := range []string{bundle, state} {
		if err := os.Mkdir(dir, 0700); err != nil {
			t.Fatal(err)
		}
	}
	if reset, err := popotWorkloadWasReset(bundle, state); err != nil || !reset {
		t.Fatalf("reset directories: reset=%v err=%v", reset, err)
	}
	for _, dir := range []string{bundle, state} {
		file := filepath.Join(dir, "container-state")
		if err := os.WriteFile(file, []byte("state"), 0600); err != nil {
			t.Fatal(err)
		}
		if reset, err := popotWorkloadWasReset(bundle, state); err != nil || reset {
			t.Fatalf("nonempty %s must still terminate: %v %v", dir, reset, err)
		}
		if err := os.Remove(file); err != nil {
			t.Fatal(err)
		}
	}
}

func TestWorkloadResetRejectsSymlink(t *testing.T) {
	root := t.TempDir()
	link := filepath.Join(t.TempDir(), "link")
	if err := os.Symlink(root, link); err != nil {
		t.Fatal(err)
	}
	if reset, err := popotWorkloadWasReset(link, root); err == nil || reset {
		t.Fatal("symlink must not be used as proof of completed reset")
	}
}
