---
title: UCM-Bridge Console
emoji: 🔀
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Bidirectional Unified Communications migration control plane
---

# UCM-Bridge console

Operator console and control plane for a bidirectional Unified Communications
migration platform. Moves configuration and identity state between on-prem
platforms (Cisco CUCM, Avaya Aura, Skype for Business Server) and cloud
platforms (Microsoft Teams Phone, Slack, Genesys Cloud CX), in both directions.

**This is a demonstration deployment.** It carries no vendor credentials and
reaches no network. Every connector runs against recorded cassettes, so the
numbers on every screen are produced by the real discovery, assessment, mapping,
planning, execution, validation, and audit code — but no real system is touched.

Two estates are available from the selector at the top:

- **Contoso — CUCM to Teams Phone** runs the whole pipeline to dry-run and is
  then correctly refused a production write, because the Teams cassettes are
  hand-authored rather than captured from a real system. The refusal is the
  point; it is shown rather than hidden.
- **Contoso — reference platform round trip** has a genuinely verified API
  surface, so a run can be executed, validated, and rolled back for real.

Use the role selector to see the RBAC boundaries: a Planner cannot execute, an
Operator cannot approve, and the two-person rule cannot be satisfied by one
person holding both.

State is in-process and resets when the Space sleeps.

---

## This file

This is the README for a Hugging Face Space, not for the project. The YAML
frontmatter above is what tells Spaces to build the `Dockerfile` and route
traffic to port 7860; without it the Space will not start.

It is already in place on the `space` branch, which is `main` with `README.md`
swapped for this file. See [`deploy/README.md`](../README.md) for the walkthrough.
