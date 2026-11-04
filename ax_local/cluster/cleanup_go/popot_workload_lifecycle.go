package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

const popotTerminalFence = "popot-terminated"

func popotWorkloadTerminated(actorDir string) bool {
	_, err := os.Lstat(filepath.Join(actorDir, popotTerminalFence))
	return !errors.Is(err, os.ErrNotExist)
}

// The file lock survives overlapping atelet processes during a rollout. The
// fence lives outside resetActorDirs, so delayed Run/Restore cannot resurrect
// a UID after terminal deletion. Checkpoint/suspend do not create this fence.
func popotBeginWorkloadOperation(ctx context.Context, actorDir string, terminate bool) (func(), error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(actorDir, 0700); err != nil {
		return nil, err
	}
	if terminate {
		marker, err := os.OpenFile(filepath.Join(actorDir, popotTerminalFence), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil && !errors.Is(err, os.ErrExist) {
			return nil, fmt.Errorf("persisting terminal fence: %w", err)
		}
		if marker != nil {
			if err := marker.Close(); err != nil {
				return nil, err
			}
		}
	}
	lock, err := os.OpenFile(filepath.Join(actorDir, "popot-lifecycle.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	for {
		if err = ctx.Err(); err != nil {
			lock.Close()
			return nil, err
		}
		err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			break
		}
		if !errors.Is(err, syscall.EWOULDBLOCK) && !errors.Is(err, syscall.EAGAIN) {
			lock.Close()
			return nil, err
		}
		timer := time.NewTimer(10 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			lock.Close()
			return nil, ctx.Err()
		case <-timer.C:
		}
	}
	release := func() { lock.Close() } // Closing releases the flock, including on process exit.
	if !terminate && popotWorkloadTerminated(actorDir) {
		release()
		return nil, fmt.Errorf("actor UID is terminally stopped; refuse delayed Run/Restore")
	}
	return release, nil
}
