package main

import (
	"errors"
	"fmt"
)

// An operation may fail after removing its container state. Confirm absence
// with runsc list -quiet, which enumerates IDs without loading or skipping
// corrupt containers. Failed inspection is never proof of successful cleanup.
func popotIgnoreRemovedContainer(name string, original error, list func() ([]string, error)) error {
	ids, err := list()
	if err != nil {
		return errors.Join(original, fmt.Errorf("checking remaining runsc containers: %w", err))
	}
	for _, id := range ids {
		if id == name {
			return original
		}
	}
	return nil
}
