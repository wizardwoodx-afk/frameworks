# Release archives

Committed build outputs, so they are downloadable straight from the repository
without running anything. They are generated — never hand-edited.

| Archive | Files | Contents |
|---|---|---|
| [`agent-frameworks-merged-1.1.0.zip`](agent-frameworks-merged-1.1.0.zip) | 97 | The **full merged framework**: the RISTER package with its tests and examples, the six upstream pins (`MANIFEST.json`, `.gitmodules`), all design docs, the debranding audit, the verification log, CI, smoke tests and the build scripts. |
| `rister-1.1.0.zip` | 71 | Just the framework as a pip-installable project. Built on demand: `make package`. |

Each archive contains `ARCHIVE.md` (what is inside) and `MANIFEST.txt`
(a SHA-256 for every file). Verify a download:

```bash
sha256sum agent-frameworks-merged-1.1.0.zip
# 46005658334f61f4adeca0e165296e1bebec65ecbabf496e53e76de4a9688df6
```

```
46005658334f61f4adeca0e165296e1bebec65ecbabf496e53e76de4a9688df6  agent-frameworks-merged-1.1.0.zip
```

## These are checked against the source

A committed archive that silently drifts from the code is worse than no archive.
`rister/tests/test_architecture.py` opens the committed zip and compares every
member against the working tree. The `releases/` directory itself is never packed
into the archive: an archive that contains its own checksum is unbuildable. Edit the framework without rebuilding and the
suite fails with the file that changed:

```bash
python3 scripts/package.py --scope repo --out releases   # rebuild
```

The build is reproducible — sorted entries, fixed timestamps, uniform
permissions — so rebuilding unchanged source must reproduce the same bytes.
