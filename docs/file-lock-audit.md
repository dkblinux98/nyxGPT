# File Lock Ordering Audit

**Date**: 2026-01-09
**Issue**: #2836
**Auditor**: developer-agent

## Summary

Comprehensive audit of all `file_lock()` usage in the myGPT codebase to ensure consistent alphabetical lock ordering is followed everywhere to prevent deadlock scenarios.

**Result**: ✅ **PASS** - All file locking follows correct ordering requirements.

## Scope

Searched for:
- `file_lock()` function calls
- Low-level `fcntl.flock()` and `msvcrt.locking()` calls
- Thread lock usage (`threading.Lock`)

## Findings

### Production Code

#### src/mygpt/sessions.py

**Lock Ordering Documentation** (lines 23-34):
- ✅ Clear documentation of alphabetical ordering requirement
- ✅ Example code showing correct usage
- ✅ Explains deadlock prevention rationale

**file_lock() Function** (lines 37-106):
- ✅ Function definition and implementation
- ✅ Uses low-level `fcntl.flock()`/`msvcrt.locking()` internally
- ✅ Single-lock usage only (no ordering concerns)

**Multiple Lock Usage** (lines 946-982):
- **Location**: `sync_filename_with_title()` function
- **Lock count**: 1-2 locks (session file always, metadata file conditionally)
- **Ordering**: ✅ **CORRECT**
  ```python
  files_to_lock = [sf]
  meta_existed_initially = mf.exists()
  if meta_existed_initially:
      files_to_lock.append(mf)
  files_to_lock.sort(key=lambda p: str(p))  # Alphabetical order by path

  if len(files_to_lock) == 2:
      with file_lock(files_to_lock[0], timeout=10.0), file_lock(files_to_lock[1], timeout=10.0):
          # ... operations ...
  ```
- **Analysis**: Files sorted alphabetically before lock acquisition, ensuring consistent ordering

**Single Lock Usage**:
- ✅ All other `file_lock()` calls acquire only one lock (no ordering concerns)

#### src/mygpt/rate_limiter.py

**Thread Locks** (lines 64, 73, 140):
- Type: `threading.Lock` (in-memory synchronization)
- **Not file locks** - no cross-process concerns
- ✅ No ordering requirements needed

### Test Code

#### tests/unit/test_sessions.py

**Single Lock Usage** (lines 397, 404, 416, 421, 431, 435, 457, 483):
- ✅ All test cases use single `file_lock()` calls
- ✅ No ordering concerns

**Monkeypatch Wrapper** (lines 585, 591):
- Used to simulate race conditions in tests
- ✅ Not a separate lock usage

**Lock Ordering Test** (lines 527-558):
- ✅ Test `test_file_lock_ordering_prevents_deadlock` exists
- **Note**: See issue #2838 for test improvement requirements

#### tests/unit/test_rate_limiter.py

**Thread Lock** (line 300):
- Type: `threading.Lock` (in-memory synchronization)
- ✅ Not a file lock

## Lock Usage Summary

| Location | Lock Type | Count | Ordering | Status |
|----------|-----------|-------|----------|--------|
| sessions.py:957 | file_lock | 2 | Alphabetical (sorted) | ✅ CORRECT |
| sessions.py:974 | file_lock | 1 | N/A | ✅ CORRECT |
| rate_limiter.py | threading.Lock | N/A | N/A | ✅ N/A |

## Verification

### Alphabetical Ordering Implementation

The `sync_filename_with_title()` function correctly implements alphabetical ordering:

1. **Collection**: Gathers all files that need locking
2. **Sorting**: `files_to_lock.sort(key=lambda p: str(p))`
3. **Acquisition**: Locks acquired in sorted order: `file_lock(files_to_lock[0])`, `file_lock(files_to_lock[1])`

### Deadlock Prevention

Alphabetical ordering prevents deadlock because:
- **Consistent order**: All processes lock in same sequence (file path alphabetical)
- **No circular wait**: Process A cannot wait for Process B's lock while holding lock B needs
- **Example**:
  - Session file: `/sessions/abc.json`
  - Metadata file: `/sessions/abc.json.meta`
  - Both processes lock in order: `abc.json` → `abc.json.meta`

## Known Issues

### Related Sub-Issues

- **#2837** (Medium): Redundant `mf.exists()` checks inside lock context
  - Lines 965, 970 check `mf.exists()` after lock acquired
  - Once locked, file always exists (O_CREAT flag)
  - Should use `meta_existed_initially` only

- **#2838** (Medium): Improve lock ordering test
  - Current test doesn't verify concurrent access or ordering
  - Should use threading to demonstrate deadlock prevention

- **#2839** (Medium): Use `p.as_posix()` for cross-platform consistency
  - Line 953 uses `str(p)` which has platform-specific separators
  - Should use `p.as_posix()` for consistent forward-slash paths

## Recommendations

### ✅ Immediate Actions: NONE

No critical violations found. All file locking follows documented alphabetical ordering.

### 📋 Follow-up Actions

1. ✅ Resolve #2837 - Remove redundant existence checks
2. ✅ Resolve #2838 - Improve test coverage for concurrent access
3. ✅ Resolve #2839 - Use `as_posix()` for cross-platform consistency

## Conclusion

**Audit Status**: ✅ **COMPLETE**

All `file_lock()` usage in the myGPT codebase follows correct alphabetical lock ordering requirements. The single location with multiple locks (`sync_filename_with_title`) correctly sorts files before acquisition, preventing deadlock scenarios.

**No violations found.**

---

**References**:
- Lock ordering documentation: `src/mygpt/sessions.py:23-34`
- Multiple lock implementation: `src/mygpt/sessions.py:946-982`
- Parent issue: #2801
- Sub-issues: #2837, #2838, #2839
