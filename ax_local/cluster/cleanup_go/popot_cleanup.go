package main

import (
	"fmt"
	"os"
)

// popotWorkloadWasReset recognizes the completed prefix of resetActorDirs:
// bundles and runsc state were removed and recreated before durable-dir failed.
// Missing paths or unreadable directories are not proof of successful teardown.
func popotWorkloadWasReset(bundleDir, stateDir string) (bool, error) {
	for _, path := range []string{bundleDir, stateDir} {
		info, err := os.Lstat(path)
		if err != nil {
			return false, err
		}
		if !info.IsDir() {
			return false, fmt.Errorf("cleanup path is not a directory: %s", path)
		}
		entries, err := os.ReadDir(path)
		if err != nil {
			return false, err
		}
		if len(entries) != 0 {
			return false, nil
		}
	}
	return true, nil
}
