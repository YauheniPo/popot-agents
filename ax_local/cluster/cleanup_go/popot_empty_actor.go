package main

import (
	"fmt"
	"os"
)

// Only the lifecycle lock and terminal fence may remain when no sandbox was
// initialized. Missing metadata alone must never hide remaining runtime state.
func popotWorkloadHasNoState(actorDir string) (bool, error) {
	info, err := os.Lstat(actorDir)
	if err != nil {
		return false, err
	}
	if !info.IsDir() {
		return false, fmt.Errorf("actor cleanup path is not a directory: %s", actorDir)
	}
	entries, err := os.ReadDir(actorDir)
	if err != nil {
		return false, err
	}
	if len(entries) != 2 {
		return false, nil
	}
	for _, entry := range entries {
		if (entry.Name() != "popot-lifecycle.lock" && entry.Name() != popotTerminalFence) || !entry.Type().IsRegular() {
			return false, nil
		}
	}
	return true, nil
}
