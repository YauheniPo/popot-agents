package controlapi

import (
	"context"
	"errors"
	"fmt"

	"github.com/agent-substrate/substrate/cmd/ateapi/internal/store"
	"github.com/agent-substrate/substrate/internal/resources"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Hold the reconciler's template lease through golden teardown AND template
// deletion. Otherwise a queued reconciliation can start a new golden workload
// after cleanup, or a deleted template can strand a running warmup actor.
func (s *RPCService) popotPrepareTemplateDeletion(ctx context.Context, ref resources.ActorTemplateRef) (context.Context, func(), error) {
	lease, err := s.impl.AcquireLease(ctx, "lease:actortemplate:"+ref.Atespace+":"+ref.Name)
	if err != nil {
		if errors.Is(err, store.ErrLeaseConflict) {
			return nil, nil, status.Error(codes.Aborted, "actor template reconciliation in progress; retry deletion")
		}
		return nil, nil, err
	}
	ctx = lease.Context()
	tmpl, err := s.impl.GetActorTemplate(ctx, ref)
	if err != nil {
		lease.Close()
		if errors.Is(err, store.ErrNotFound) {
			err = status.Errorf(codes.NotFound, "ActorTemplate %s not found", ref)
		}
		return nil, nil, err
	}
	golden := resources.ActorRef{Atespace: resources.GoldenActorAtespace, Name: tmpl.GetMetadata().GetUid()}
	if _, err := s.actorWorkflow.DeleteActor(ctx, golden, true); err != nil && status.Code(err) != codes.NotFound {
		lease.Close()
		return nil, nil, fmt.Errorf("terminating template golden actor: %w", err)
	}
	return ctx, func() { lease.Close() }, nil
}
