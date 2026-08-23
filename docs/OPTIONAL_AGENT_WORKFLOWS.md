# Optional agent workflows

## Superpowers brainstorming

[`superpowers:brainstorming`](https://github.com/obra/superpowers) is a useful
optional discovery workflow for mechanical-design work. It helps a coding agent
clarify design intent, requirements, constraints, alternatives, acceptance
criteria, and approval boundaries before implementation begins.

It is maintained by Jesse Vincent as part of the external Superpowers project
and is distributed under the MIT License. This repository does not vendor,
relicense, install, or configure Superpowers.

### When to consider it

Consider the skill when a request is new, ambiguous, multi-stage, or likely to
change mechanical interfaces. Relevant discovery topics can include function,
operating sequence, units, dimensional envelope, loads, duty cycle, motion,
materials, environment, standards, standard parts, fits, tolerances,
manufacturing constraints, maintenance, safety, and validation criteria.

Routine inspection, an already-approved parameter change, or a deterministic
validation run does not need to be blocked merely because the optional skill is
absent.

### Installation

Installation is always a deliberate user action because plugins modify the
user's agent environment and installation differs by harness.

For the Codex App:

1. Open **Plugins** in the sidebar.
2. Find **Superpowers** in the Coding category.
3. Choose **Install** and review the prompts.

For Codex CLI:

1. Run `/plugins`.
2. Search for `superpowers`.
3. Select **Install Plugin**.

For other supported coding agents, follow the current instructions in the
[official Superpowers repository](https://github.com/obra/superpowers).

### Project behavior

- Superpowers is optional and is not a runtime or packaging dependency.
- The project must never install or update it automatically.
- The absence or rejection of the plugin must not block setup, CAD operations,
  validation, or delivery.
- When it is unavailable, the coding agent should perform proportionate
  requirements clarification directly in the conversation.
