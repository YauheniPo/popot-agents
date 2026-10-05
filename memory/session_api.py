"""Authenticated lifecycle bridge for the pinned Cognee dashboard SDK.

Upstream exposes session traces over HTTP, but lifecycle completion and external
model usage only through Python. Extend its app without changing vendor files.
"""
from typing import Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from cognee.api.client import app
from cognee.infrastructure.session.get_session_manager import get_session_manager
from cognee.modules.data.methods import get_datasets_by_name
from cognee.modules.session_lifecycle.metrics import (
    SessionStatus, accumulate_usage, ensure_and_touch_session, get_session_row, mark_ended,
)
from cognee.modules.users.methods import get_authenticated_user
from cognee.modules.users.models import User


class ToolStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=200, pattern=r'^[a-zA-Z0-9_.:-]+$')
    status: Literal['success', 'error']
    output_chars: int = Field(default=0, ge=0, le=100000000)


class SessionEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    session_id: str = Field(pattern=r'^popot-[a-z0-9-]{1,80}$')
    dataset_name: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    role: str = Field(default='agent', pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    status: Literal['running', 'completed', 'failed']
    tools: list[ToolStep] = Field(default_factory=list, max_length=200)
    model: str = Field(default='', max_length=200)
    tokens_in: int = Field(default=0, ge=0, le=100000000)
    tokens_out: int = Field(default=0, ge=0, le=100000000)
    cost_usd: float = Field(default=0, ge=0, le=100000)
    cost_reported: bool = False
    memories_saved: int = Field(default=0, ge=0, le=5)


@app.post('/api/v1/popot/session')
async def session_event(payload: SessionEvent, user: User = Depends(get_authenticated_user)):
    # Only an owned dataset may be attributed; never guess another user's brain.
    datasets = await get_datasets_by_name([payload.dataset_name], user.id)
    dataset_id = datasets[0].id if datasets else None
    row = await get_session_row(session_id=payload.session_id, user_id=user.id)
    if row is not None and row.status in ('completed', 'failed'):
        return {'status': row.status, 'session_id': payload.session_id}
    await ensure_and_touch_session(session_id=payload.session_id, user_id=user.id,
                                   dataset_id=dataset_id)
    sm = get_session_manager(dataset_id=dataset_id)
    for tool in payload.tools:
        await sm.add_agent_trace_step(user_id=str(user.id), session_id=payload.session_id,
            origin_function=tool.name, status=tool.status, generate_feedback_with_llm=False,
            method_return_value={'output_chars': tool.output_chars})
    if payload.status != 'running':
        await accumulate_usage(session_id=payload.session_id, user_id=user.id,
            tokens_in=payload.tokens_in, tokens_out=payload.tokens_out,
            cost_usd=payload.cost_usd if payload.cost_reported else 0,
            model=payload.model or None)
    await sm.add_agent_trace_step(user_id=str(user.id), session_id=payload.session_id,
        origin_function='popot.' + payload.role + '.' + payload.status,
        status='error' if payload.status == 'failed' else 'success',
        generate_feedback_with_llm=False,
        method_return_value={'project': payload.dataset_name,
            'memories_saved': payload.memories_saved,
            'cost_reported': payload.cost_reported,
            'cost_note': 'provider-reported' if payload.cost_reported else 'not reported by provider'})
    if payload.status != 'running':
        await mark_ended(session_id=payload.session_id, user_id=user.id,
                         status=SessionStatus(payload.status))
    return {'status': payload.status, 'session_id': payload.session_id}
