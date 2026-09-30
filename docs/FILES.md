# Files

Nox can look at and change files in the folders you name, and nowhere else.

The list ships **empty**. A fresh installation can read, write and move nothing outside its own
vault, and the tools say which setting to add a folder to rather than failing in some confusing way.

```yaml
files:
  roots:
    - "%USERPROFILE%/Downloads"
    - "%USERPROFILE%/Documents/Notizen"
```

## Why an empty list means "nowhere"

Because the other reading is how a guard meant to restrict ends up permitting. An empty allow-list
that is treated as "no restrictions" is a classic, and it would mean a default installation where
`file.read` reaches your whole disk.

## How a path is checked

The path is **resolved first and checked second**. `Path.resolve()` follows `..`, symlinks and
Windows junctions, so the check is against where the path really leads:

- `Downloads/../../Windows/System32` - refused.
- A junction inside `Downloads` that points at `C:/Users` - refused. There are no dots to notice in
  that one, which is why resolving first matters.
- A file that does not exist yet - allowed for a write, as long as its folder is inside a root.

There is a test for each of those, and the junction test creates a real junction.

## What each tool may do

| Tool | Risk | Notes |
| --- | --- | --- |
| `file.roots` | read | Which folders exist. Ask before guessing a path. |
| `file.list` | read | Names, sizes, dates. Capped, and says when a listing was cut. |
| `file.read` | read | Text only, capped. Refuses pictures and binaries. |
| `file.write` | medium | Asks first. Never replaces a file unless told to, never creates folders. |
| `file.move` | medium | Asks first. Both ends inside a root. |
| `file.delete` | high | Asks first, and only into the Recycle Bin. |

"Asks first" is the permission engine's default for that risk level in every profile that has not
explicitly allowed the tool. A profile rule can narrow by folder, because each tool reports its path
as the permission target: *writing is fine under Downloads, ask me anywhere else* is a rule, not a
code change.

## Deleting

There is no delete that cannot be undone. `file.delete` goes through the Windows shell with
`FOF_ALLOWUNDO`, which is what puts a file in the Recycle Bin; `os.unlink` does not, whatever flags
it is given. If the shell call is unavailable or fails, the file stays and the tool says so.

Turning `files.delete_to_recycle_bin` off does not switch to a plain delete - it removes the delete
tool entirely.

## Limits you can change

| Setting | Default | Why there is a limit at all |
| --- | --- | --- |
| `max_read_bytes` | 262144 | A file read becomes part of a prompt. One wrong path would otherwise make one very expensive turn. |
| `max_list_entries` | 200 | A folder with 40000 files must not become one answer. |
| `max_write_bytes` | 262144 | Bounds a runaway loop. |

## What Nox still cannot do with files

Everything outside `files.roots`, and:

- run a program that is not a registered preset action,
- change file permissions or owners,
- read a file that is not text (no PDF or image extraction yet).

Ask Nox - `capabilities.check` answers from a list rather than guessing, and says "no entry for
that" when it does not know.
