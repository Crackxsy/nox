# Asking Nox to change itself

Nox can write a proposed change to its own source, run your tests against it, and show you the
diff. It cannot apply one.

That split is the whole feature. Merging a branch and restarting Nox on the result are things you
do with your own git, which is what keeps "Nox can extend itself" from meaning "Nox can change what
it is while you are not looking".

## Switching it on

Nothing happens until you name a checkout:

```yaml
extend:
  workspace: "E:/Nox/repo"          # a git repository. Empty = Nox may not touch anything
  test_command:                     # absolute path first, the way preset actions work
    - "E:/Nox/repo/.venv/Scripts/python.exe"
    - "-m"
    - "pytest"
    - "tests/unit"
    - "-q"
```

You also need the **coding profile**. The plugin that writes the change only runs there - its
manifest says `profiles: [coding]`, and the plugin manager refuses to spawn it under any other
profile before a subprocess exists. Asking from the companion profile gets you a sentence saying so,
not a silent failure.

## What happens when you ask

1. Nox asks you first. `extend.propose` is high risk, so every profile that has not explicitly
   allowed it opens a confirmation - and the confirmation shows what the change is *for*, not an
   identifier.
2. It starts a branch at your current HEAD: `nox/proposal/<id>`.
3. The `coding` plugin writes the change there. That is a Claude Code session with Read, Edit,
   Write, Glob and Grep - deliberately no shell, and scoped to the workspace.
4. It commits whatever the session wrote.
5. It runs your `test_command` in the workspace.
6. It goes back to the branch you were on, and tells you what it found.

Step 6 happens on **every** path out, including the ones that failed. A proposal that fell over
halfway must not leave your repository sitting on a branch you did not make.

## What you get back

| State | Meaning |
| --- | --- |
| `ready` | There is a diff and the tests passed. |
| `tests_failed` | There is a diff and the tests did not. The branch stays - a red diff is still evidence. |
| `empty` | The session ran and changed nothing. Not a failure, just nothing to read. |
| `failed` | It did not get as far as a diff. The note says why. |

`extend.status` lists the files that changed, git's own one-line summary, and the tail of the test
output - which is where a test runner says what broke.

With no `test_command` configured, a proposal says *"no test command is configured, so nothing was
run"* rather than implying that silence means it passed.

## What it will never do

No merge. No push. No `reset --hard`. No branch deletion. No restarting itself. The git surface is
four verbs, and the absences are deliberate.

It also cannot run a shell: the coding plugin's allowed tools are Read, Edit, Write, Glob and Grep,
and the only command Nox runs itself is the test command *you* configured, with an absolute path -
the same rule preset actions follow, and for the same reason.

## Reading a proposal

```
git -C <workspace> log --oneline nox/proposal/<id>
git -C <workspace> diff develop...nox/proposal/<id>
```

If you want it, merge it like any other branch and restart Nox. If you do not, delete the branch.
Nox will not do either for you.
