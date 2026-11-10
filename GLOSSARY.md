# Popot Agents

Popot Agents assigns work to AI agents with defined capabilities, maintains their
sessions, and coordinates contributions from specialist roles.

## Language

### Agents and capabilities

**Agent**:
An AI participant carrying out work with a model, instructions, and available tools.
_Avoid_: model, agent profile, worker.

**Role**:
A named specialization defining an agent's responsibilities, instructions,
permitted tools, skills, and delegation relationships.
_Avoid_: agent profile, model.

**Agent profile**:
A named configuration for accessing a model provider that can be shared by several roles.
_Avoid_: role, agent.

**Skill**:
A reusable set of instructions for a type of work, used within a role's existing capabilities.
_Avoid_: tool, permission.

### Work and coordination

**Task**:
A requested unit of work with a defined scope and intended result, assigned by a
user or a parent agent.
_Avoid_: AX Task, session, workflow run.

**Workflow**:
The coordination process that carries a task from an authorized request to a
final outcome, including any delegated work and verification.
_Avoid_: task, session.

**Workflow run**:
One bounded execution of a workflow for a task within a session, potentially
spanning several coordination turns. A session can contain successive workflow runs.
_Avoid_: session, coordination turn.

**Coordination turn**:
A bounded segment of an agent's work within a workflow run, potentially involving
several model requests and tool calls.
_Avoid_: user message, model request, workflow run.

**Delegation**:
A parent agent's assignment of scoped work to another agent in a permitted role,
with the result returned to the parent. The child has its own session and capabilities.
_Avoid_: role switch, shared session.

### Sessions and execution

**Session**:
A persistent conversation context containing message history and the agent
settings chosen for that conversation. A session can outlive the worker serving it.
_Avoid_: worker, task, workflow run.

**Worker**:
An execution instance that hosts an agent's work for a session and can be suspended
or replaced while the session remains.
_Avoid_: agent, session.

**Sandbox**:
The isolated execution environment in which a worker runs and keeps its workspace.
_Avoid_: session, workspace.

**Workspace**:
The working files available in an agent's sandbox, with a lifetime separate from
the conversation history.
_Avoid_: session, sandbox.

**AX Task**:
A runtime-managed execution resource hosting a worker and its sandbox. One session
can use successive AX Tasks over its lifetime.
_Avoid_: task, session.
