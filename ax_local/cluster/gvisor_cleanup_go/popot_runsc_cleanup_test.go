package main

import (
	"errors"
	"testing"
)

func TestRemovedContainersAllowCleanupToContinue(t *testing.T) {
	for _, name := range []string{"guest", "_pause"} {
		err := popotIgnoreRemovedContainer(name, errors.New("runtime operation failed"), func() ([]string, error) {
			if name == "guest" {
				return []string{"_pause"}, nil
			}
			return nil, nil
		})
		if err != nil {
			t.Fatalf("already removed %s: %v", name, err)
		}
	}
}

func TestExistingOrUninspectableContainerPreservesFailure(t *testing.T) {
	original := errors.New("permission denied or invalid runtime state")
	inspection := errors.New("container list failed")
	for _, test := range []struct {
		name    string
		ids     []string
		listErr error
	}{
		{"still present", []string{"guest", "_pause"}, nil},
		{"inspection failed", nil, inspection},
	} {
		t.Run(test.name, func(t *testing.T) {
			err := popotIgnoreRemovedContainer("guest", original, func() ([]string, error) {
				return test.ids, test.listErr
			})
			if !errors.Is(err, original) {
				t.Fatalf("original failure lost: %v", err)
			}
			if test.listErr != nil && !errors.Is(err, inspection) {
				t.Fatalf("inspection failure lost: %v", err)
			}
		})
	}
}
