# Portable smoke readiness contract (schema 1)

This opt-in observation boundary is active only when all runner environment
variables below are present and the JSON scope marker validates. The runner
creates the marker exclusively beside its disposable application copy; normal
installed folders have no marker and must never be changed by inherited smoke
variables.

```text
OMR_GRADER_SMOKE_READY_FILE=<absolute, initially absent sibling path>
OMR_GRADER_SMOKE_MODE=writable|readonly
OMR_GRADER_SMOKE_PHASE=write|read|readonly
OMR_GRADER_SMOKE_NONCE=<random nonce>
OMR_GRADER_SMOKE_SCOPE_MARKER=<absolute sibling marker path>
```

The marker is UTF-8 JSON with exact fields `schema: 1`, `nonce`, `app_root`,
and `ready_file`. `app_root` and `ready_file` must be the canonical absolute
paths supplied above; the nonce must match. The application must exclusively
and atomically create the ready file only after the real `mainWindow` is visible
and the Qt event loop has entered (a queued observation). A splash title is
never readiness, and the hook must not overwrite an arbitrary pre-existing
outside file.

The ready object has these required fields:

```json
{
  "schema": 1,
  "pid": 1234,
  "state": "main-ready",
  "read_only": false,
  "write_enabled": true,
  "affordances": {"config_persistence": true, "session_persistence": true},
  "persistence_roundtrip": false,
  "persistence": {"default_sensitivity": 7, "config_sha256": "lower-case SHA-256"}
}
```

For writable phase `write`, use the ordinary settings save path to persist a
nondefault sensitivity (not 5); emit `persistence.phase: "written"`,
`persistence_roundtrip: false` and the
authoritative saved value/hash. For phase `read`, reload through the normal
application path, compare its authoritative value/hash with the saved state,
then emit `persistence.phase: "reopened"` with the same stable
`default_sensitivity` and `config_sha256` values and `persistence_roundtrip: true`.
The harness compares both values, so a hardcoded true or file-existence check
does not pass. In readonly phase, write access and both affordances are false,
`persistence_roundtrip` is false, and `persistence` is null; no portable-root
bytes may change.
