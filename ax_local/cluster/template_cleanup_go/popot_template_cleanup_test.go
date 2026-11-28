package controlapi

import (
	"context"
	"errors"
	"testing"

	"github.com/agent-substrate/substrate/cmd/ateapi/internal/store"
	"github.com/agent-substrate/substrate/cmd/ateapi/internal/store/storetest"
	"github.com/agent-substrate/substrate/internal/resources"
	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type popotFailGoldenStore struct{ actorWorkflowStore }

func (s popotFailGoldenStore) GetActor(context.Context, resources.ActorRef) (*ateapipb.Actor, error) {
	return nil, errors.New("golden cleanup unavailable")
}

type popotObserveDeletion struct {
	serviceStore
	t      *testing.T
	golden resources.ActorRef
}

func (s popotObserveDeletion) DeleteActorTemplate(ctx context.Context, ref resources.ActorTemplateRef) (*ateapipb.ActorTemplate, error) {
	if _, err := s.GetActor(ctx, s.golden); !errors.Is(err, store.ErrNotFound) {
		s.t.Fatal("template removed before golden teardown")
	}
	lease, err := s.AcquireLease(ctx, "lease:actortemplate:"+ref.Atespace+":"+ref.Name)
	if lease != nil {
		lease.Close()
	}
	if !errors.Is(err, store.ErrLeaseConflict) {
		s.t.Fatalf("template lease released before record deletion: %v", err)
	}
	return s.serviceStore.DeleteActorTemplate(ctx, ref)
}

type popotDeletionFixture struct {
	st     store.Interface
	svc    *RPCService
	ref    resources.ActorTemplateRef
	golden resources.ActorRef
	req    *ateapipb.DeleteActorTemplateRequest
}

func newPopotDeletionFixture(t *testing.T, mode string) popotDeletionFixture {
	t.Helper()
	ctx := context.Background()
	st, cleanup := storetest.SetupTestStore(t)
	t.Cleanup(cleanup)
	ref := resources.ActorTemplateRef{Atespace: "team-a", Name: "popot-template"}
	workflow := newTestActorWorkflow(t, st, ref.Atespace, ref.Name)
	tmpl, err := st.GetActorTemplate(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	golden := resources.ActorRef{Atespace: resources.GoldenActorAtespace, Name: tmpl.GetMetadata().GetUid()}
	if mode != "absent" {
		seedWorkflowActor(t, ctx, st, golden, ref.Atespace, ref.Name, ateapipb.ActorState_ACTOR_STATE_RUNNING)
	}
	if mode == "busy" {
		lease, err := st.AcquireLease(ctx, "lease:actortemplate:"+ref.Atespace+":"+ref.Name)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { lease.Close() })
	}
	if mode == "cleanup-failure" {
		workflow.store = popotFailGoldenStore{workflow.store}
	}
	svc := &RPCService{impl: popotObserveDeletion{newServiceImpl(st, nil), t, golden}, actorWorkflow: workflow}
	req := &ateapipb.DeleteActorTemplateRequest{ActorTemplate: &ateapipb.ObjectRef{Atespace: ref.Atespace, Name: ref.Name}}
	return popotDeletionFixture{st, svc, ref, golden, req}
}

func (f popotDeletionFixture) checkBlocked(t *testing.T, mode string, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("expected deletion to fail")
	}
	if mode == "busy" && status.Code(err) != codes.Aborted {
		t.Fatalf("busy lease: %v", err)
	}
	ctx := context.Background()
	if _, err := f.st.GetActorTemplate(ctx, f.ref); err != nil {
		t.Fatalf("lost template: %v", err)
	}
	if _, err := f.st.GetActor(ctx, f.golden); err != nil {
		t.Fatalf("lost golden: %v", err)
	}
}

type popotLeaseFailure struct {
	serviceStore
	err error
}

func (s popotLeaseFailure) AcquireLease(context.Context, string) (*store.Lease, error) {
	return nil, s.err
}

func TestPopotTemplateDeletion(t *testing.T) {
	t.Run("lease-store-error", func(t *testing.T) {
		failure := errors.New("lease storage unavailable")
		svc := &RPCService{impl: popotLeaseFailure{err: failure}}
		_, release, err := svc.popotPrepareTemplateDeletion(context.Background(), resources.ActorTemplateRef{})
		if !errors.Is(err, failure) || release != nil {
			t.Fatalf("unexpected lease result: %v", err)
		}
	})
	for _, mode := range []string{"running", "absent", "busy", "cleanup-failure"} {
		t.Run(mode, func(t *testing.T) { checkPopotTemplateDeletion(t, mode) })
	}
}

func checkPopotTemplateDeletion(t *testing.T, mode string) {
	t.Helper()
	f := newPopotDeletionFixture(t, mode)
	ctx := context.Background()
	_, err := f.svc.DeleteActorTemplate(ctx, f.req)
	if mode == "busy" || mode == "cleanup-failure" {
		f.checkBlocked(t, mode, err)
		return
	}
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.st.GetActor(ctx, f.golden); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("golden survives deletion: %v", err)
	}
	if _, err := f.st.GetActorTemplate(ctx, f.ref); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("template survives deletion: %v", err)
	}
	if _, err := f.svc.DeleteActorTemplate(ctx, f.req); status.Code(err) != codes.NotFound {
		t.Fatalf("repeat delete: %v", err)
	}
}
