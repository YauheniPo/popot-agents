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

func TestPopotTemplateDeletion(t *testing.T) {
	for _, mode := range []string{"running", "absent", "busy", "cleanup-failure"} {
		t.Run(mode, func(t *testing.T) {
			ctx := context.Background()
			st, cleanup := storetest.SetupTestStore(t)
			defer cleanup()
			workflow := newTestActorWorkflow(t, st, "team-a", "popot-template")
			ref := resources.ActorTemplateRef{Atespace: "team-a", Name: "popot-template"}
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
				defer lease.Close()
			}
			if mode == "cleanup-failure" {
				workflow.store = popotFailGoldenStore{workflow.store}
			}
			svc := &RPCService{impl: popotObserveDeletion{newServiceImpl(st, nil), t, golden}, actorWorkflow: workflow}
			req := &ateapipb.DeleteActorTemplateRequest{ActorTemplate: &ateapipb.ObjectRef{Atespace: ref.Atespace, Name: ref.Name}}
			_, err = svc.DeleteActorTemplate(ctx, req)
			if mode == "busy" || mode == "cleanup-failure" {
				if err == nil {
					t.Fatal("template deletion must fail while golden teardown is unconfirmed")
				}
				if mode == "busy" && status.Code(err) != codes.Aborted {
					t.Fatalf("busy lease: %v", err)
				}
				if _, err := st.GetActorTemplate(ctx, ref); err != nil {
					t.Fatalf("cleanup failure lost template: %v", err)
				}
				if _, err := st.GetActor(ctx, golden); err != nil {
					t.Fatalf("cleanup failure lost golden: %v", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if _, err := st.GetActor(ctx, golden); !errors.Is(err, store.ErrNotFound) {
				t.Fatalf("golden survives template deletion: %v", err)
			}
			if _, err := st.GetActorTemplate(ctx, ref); !errors.Is(err, store.ErrNotFound) {
				t.Fatalf("template survives deletion: %v", err)
			}
			if _, err := svc.DeleteActorTemplate(ctx, req); status.Code(err) != codes.NotFound {
				t.Fatalf("repeat delete: %v", err)
			}
		})
	}
}
