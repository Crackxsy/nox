"""Opening a path for a node's `file.*` tools: inside a folder this device allows, decided on the
real file at the moment it is opened - never on the text the core sent.

Order for every open (list, read, write):

1. **The text** (`split`): UNC and Win32 device paths, `..`, a leading `~`, 8.3 short names
   (`NOXNOD~1`), NUL, alternate data streams (`a.txt:s`), Windows device names (`CON`, `aux.txt`)
   and names Windows would silently change (a trailing dot or space) are refused before anything
   touches the file system - an SMB path is never resolved, so the device never reaches out. A
   relative path is relative to the first allowed folder; an absolute one must spell one of them.
   A write is also refused when the path is an **execution trigger** (`execution_trigger`): a
   `.git` folder (hooks), a startup or autostart folder, a shell start file, a script or program.
2. **The open, bound to a handle**:
   * POSIX - `openat`-style walk from the allowed folder (whose identity was recorded when the
     folders were built): each entry is looked at first without following it (`fstatat`
     `AT_SYMLINK_NOFOLLOW` - a link, FIFO, socket or device is refused before it is opened), then
     opened `O_NOFOLLOW | O_NOCTTY | O_NONBLOCK` relative to the directory before it, and every
     directory passed is compared with the protected folders by `(st_dev, st_ino)`.
   * Windows - `CreateFileW` without `FILE_SHARE_DELETE` (the file cannot be renamed away while it
     is open), then `GetFinalPathNameByHandleW` on that handle: the path the open really reached
     after junctions, symlinks and 8.3 names.
   A new file is created exclusively (`O_EXCL` / `CREATE_NEW`, never executable) and removed again
   when its final path is not allowed (Windows: through its own handle; POSIX: by name in the
   directory that was checked); an existing file is truncated only after the check.
3. **The verdict on the handle** (`_verify`): inside an allowed folder, not inside a protected
   one (the node's own folder; the keyring, credential and browser-profile stores of this user -
   `secret_locations`), not a hard link to a protected file, a regular file or a directory. For a
   write also: no second hard link, no executable bit, no execution trigger on the final path.

`file.move` and `file.delete` (`move_file`, `trash_file`) take the same three steps for both ends of
a move and for the file to delete, then act relative to the verified handles - a rename without
replacing, into the trash only (never an unlink); see their docstrings.

Everything read or written afterwards goes through that one handle, so swapping the path between
the check and the use changes nothing (`tests/unit/node/test_safe_fs_read_write.py` swaps it;
the example is drawn in docs/ARCHITECTURE-NODES.md, "Files: check on the handle"). What the OS
cannot bind to a handle is said plainly: listing a folder on Windows enumerates by its verified
path while the folder's handle is held open and compares the folder's identity before and after.

The package is cut along the steps above, and every module says what it owns:

* `core`, `formats`, `text_checks` - the shared refusal and path spellings; what may be written, by
  the path's text and by the text itself (step 1; no file is opened).
* `secret_places`, `workspace`, `folders` - the places that are never reached, repositories and app
  workspaces, and the allowed folders (step 3).
* `posix`, `windows`, `windows_files` - the open bound to a handle on each platform, and moving and
  deleting on Windows (step 2).
* `opening`, `move_delete` - the entry points: `open_existing`, `open_new`, `list_folder`,
  `move_file`, `trash_file`.
"""
