---
name: rgi-panel
description: Read a private RGB keyboard status panel or claim, update, and release a task's indicator lane when the user asks to use it.
---

Use this workflow when the user asks to inspect or control their RGB panel.
Explicit user instructions take precedence over this guidance.

1. Call `panel_status` to read live devices, free lanes, and this connection's
   sessions. Report the returned state; do not invent physical LED verification.
2. To track a task, choose one unique, stable `session_id` using letters, digits,
   underscores or hyphens. Reuse it throughout the task and on retries. Call
   `session_start` with a short label. Save the returned lane number and key.
3. Call `session_set_state` on meaningful transitions: `working` for active work,
   `blocked` only while the user is needed, `done` after completion, `error` for
   an actual failure, or `idle` when claimed but inactive. A successful write
   means the panel API acknowledged it; it is not proof of the physical LEDs.
4. Keep the lane after a completed turn so the user can see the white indicator.
   Release it with `session_end` when the user closes the tracked task, requests
   cleanup, or when finishing a temporary test. Do not release someone else's lane.

For a quick test, claim a fresh ID, set `working`, read status, set `done`, read
status again, and release it. Report which steps succeeded.

On a missing session, reclaim using the same ID, then send its current state.
On capacity or connectivity failure, continue the actual work without an
indicator. A write timeout has an unknown outcome: inspect status before retrying.
Do not ask for credential values in chat; the operator configures them locally.

This private connection shares one namespace with everyone allowed to use its
tunnel. It is not a multi-user authorization service. These tools do not provide
automatic ChatGPT lifecycle monitoring. Use only the state transitions you can
actually observe, and only within the user's requested workflow.
