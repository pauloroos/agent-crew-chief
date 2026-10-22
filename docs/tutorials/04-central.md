# 04-central · Core installation, launch and recovery

This tutorial documents the core installation, launch, monitoring, and recovery
workflow for agent-crew-chief v0.4.0. It depends on tutorials 01–03 and tasks
T025, T026, T027.

## Before you start

This tutorial uses the small [`examples/hello-crew/`](../../examples/hello-crew/)
project. Ensure ACC is installed and configured:

```bash
python -I .agents/skills/crew-chief/scripts/crew-chief.py setup --yes --project .
python -I .agents/skills/crew-chief/scripts/crew-chief.py discover --project .
```

## 1. Core installation

Install a released ACC bundle into a practice repository:

```bash
# Install ACC v0.4.0 into a temporary repo
python -I .agents/skills/crew-chief/scripts/crew-chief.py install . --release 0.4.0
```

The `install.py` script copies the skill into the repository and creates a
user-scoped `acc` wrapper. The old script is retained; global preferences and
PATH are never silently edited.

**Command‑install** (FR-001):

```bash
acc --command <path-to-binary>
```

This installs an explicit user‑scoped wrapper without requiring a full project
installation. The legacy `install.py` still works, and existing settings remain
unchanged.

## 2. Launching the central tool

Launch ACC in a temporary isolated worktree. No Orca executable is required
(US6/AC2):

```bash
acc launch --project . --request "# request\nShip the central tool.\n"
```

**CLI override** — force a specific executor:

```bash
acc launch --cli opencode
```

**Yolo mode** — bypass approvals:

```bash
acc launch --yolo
```

**Restricted mode** — coordinator is unrestricted but individual runs are
restricted:

```bash
acc launch --restricted
```

## 3. Monitoring and messages

Monitor an running job and send messages between rounds:

```bash
# Monitor job progress
acc monitor --job JOB_ID

# Send a message to the worktree inbox
acc message --job JOB_ID --text "What's the status?"
```

Durable file inboxes/outboxes with message IDs, acknowledgments, timestamps
and idempotent redelivery are used (FR-007, SC-003). Malformed or oversized
messages are rejected.

## 4. Attach and pause

Take exclusive ownership of a session, or pause it:

```bash
# Attach to an interactive terminal
acc attach --job JOB_ID

# Pause the current round
acc pause --job JOB_ID
```

## 5. Stop and recovery

Stop a job and recover explicitly:

```bash
# Stop the job
acc stop --job JOB_ID

# Explicit recovery — never signal a bare persisted PID or silently restart
acc recover --job JOB_ID
```

**No pty dependency** (FR-020): Windows needs no pty, curses, tmux or
ConPTY. Interactive entry uses the actual terminal and persisted session.

**Preserve v0.3.4 behavior** (FR-018): Coordinators use existing
pick/brief/dispatch/review/grade/accept/close and leave work in their own
branch for owner integration. Source checkouts are never merged or pushed
automatically.

## 6. Recovery

When a coordinator fails or a session is interrupted, recover using the
persisted session evidence:

```bash
acc recovery --job JOB_ID
```

Recovery reads the last OK attempt's branch, restores eligibility, and
reconciles stale supervisors. It never kills by PID, loses messages or
silently retries failed rounds.

## 7. Owner integration

After a job is complete, the coordinator branch retains close output. The
owner can review the diff, accept the work, or discard it. No test process
survives after cleanup — final cleanup proves no test process survives.

---

**Release documentation** (SC-011): This tutorial gives the exact
install/launch/monitor/message/attach/pause/stop/recovery flow, with split
estimates and owner‑controlled integration. No private data, client names,
personal paths or credentials appear in committed artifacts.