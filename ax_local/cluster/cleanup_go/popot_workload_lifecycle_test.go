package main

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"
)

func TestTerminalWorkloadCannotRunOrRestoreAgain(t *testing.T) {
	root := t.TempDir()
	actor := filepath.Join(root, "actor-a")
	release, err := popotBeginWorkloadOperation(context.Background(), actor, false)
	if err != nil {
		t.Fatal(err)
	}
	release()
	// An ordinary pause/restart has no terminal fence.
	release, err = popotBeginWorkloadOperation(context.Background(), actor, false)
	if err != nil {
		t.Fatal(err)
	}
	release()
	release, err = popotBeginWorkloadOperation(context.Background(), actor, true)
	if err != nil {
		t.Fatal(err)
	}
	release()
	for i := 0; i < 2; i++ {
		if release, err = popotBeginWorkloadOperation(context.Background(), actor, false); err == nil {
			release()
			t.Fatal("terminated UID restarted")
		}
	}
	// Delete retries remain valid, while another UID is unaffected.
	release, err = popotBeginWorkloadOperation(context.Background(), actor, true)
	if err != nil {
		t.Fatal(err)
	}
	release()
	release, err = popotBeginWorkloadOperation(context.Background(), filepath.Join(root, "actor-b"), false)
	if err != nil {
		t.Fatal(err)
	}
	release()
}

func TestStopIntentFencesQueuedStartAndWaitsForInFlightOperation(t *testing.T) {
	actor := filepath.Join(t.TempDir(), "actor")
	release, err := popotBeginWorkloadOperation(context.Background(), actor, false)
	if err != nil {
		t.Fatal(err)
	}
	stopped := make(chan error, 1)
	go func() {
		unlock, e := popotBeginWorkloadOperation(context.Background(), actor, true)
		if e == nil {
			unlock()
		}
		stopped <- e
	}()
	deadline := time.Now().Add(time.Second)
	for {
		if popotWorkloadTerminated(actor) {
			break
		}
		if time.Now().After(deadline) {
			release()
			t.Fatal("stop intent was not persisted while waiting")
		}
		time.Sleep(time.Millisecond)
	}
	select {
	case <-stopped:
		release()
		t.Fatal("terminate bypassed the running operation")
	default:
	}
	stale := make(chan error, 1)
	go func() {
		unlock, e := popotBeginWorkloadOperation(context.Background(), actor, false)
		if e == nil {
			unlock()
		}
		stale <- e
	}()
	release()
	if e := <-stopped; e != nil {
		t.Fatal(e)
	}
	if e := <-stale; e == nil {
		t.Fatal("queued start resurrected deleted actor")
	}
}

func TestOperationLockHonorsCanceledDeadline(t *testing.T) {
	actor := filepath.Join(t.TempDir(), "actor")
	release, err := popotBeginWorkloadOperation(context.Background(), actor, false)
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	_, err = popotBeginWorkloadOperation(ctx, actor, false)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expected deadline, got %v", err)
	}
}
