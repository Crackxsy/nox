# Plans

A plan is a few tool steps with a title, written down so you can read them before they run.

Nox proposes one when what you asked for takes more than a single step, or when it should carry on
in the background. Nothing happens until you approve it.

```
Downloads sortieren
  0. vault.read        - nachsehen, welche Ordner es gibt
  1. presets.run_action - das Sortierskript starten
  2. vault.append_inbox - notieren, was verschoben wurde
```

## What a plan can and cannot contain

Only tool names and arguments - the same tools the dashboard offers. No command lines, no
conditions, no loops, and no plans inside plans. Everything a plan can do is a tool somebody wrote
and a permission somebody granted. A plan makes work survive a restart; it does not give the
language model a longer reach.

At most twelve steps, because a plan you have to read before approving has to stay readable.

## Who may start one

| Route | What happens |
| --- | --- |
| Nox proposes (`plans.propose`) | Nothing runs. Low risk, allowed in every profile. |
| Nox starts one (`plans.start`) | High risk, so you are asked first - in every profile. |
| You approve in the dashboard | Runs. The click *is* the confirmation. |

Either way, every step still goes through the permission engine when it gets there. A step whose
tool needs confirming will ask, which means a plan full of confirmable steps is a plan to run while
you are watching: unattended, those steps time out and are recorded as refused.

Steps run under the `plans` agent, so a profile can say yes to work you approved without saying yes
to the same tool asked for in the middle of a sentence.

## While it runs, and after a restart

Approved plans live in the task queue: they are written to the database before they start, they
pause on their own while a game is running, and they resume after a crash.

A step is a tool call with side effects, so the step being attempted is recorded *before* the call.
A plan that comes back from a restart therefore marks that one step as interrupted and stops, rather
than starting a program a second time. You see a plan that reached step three and why - a visible
gap instead of an invisible repeat. Approving the rest is your decision.

`plans.status` reports every step with its result, including the ones that failed and their reason.
"The plan failed" on its own tells you nothing.

## Where plans are stored

Proposals are in memory and are gone after a restart: a proposal is part of a conversation, and a
machine that boots with a list of things a language model once suggested is clutter.

Approved plans are rows in the task database under `paths.database_dir`, payload and progress both.
