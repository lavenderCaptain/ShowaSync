# Long Filename Fix Summary

## Problem
The application was crashing with `OSError: [Errno 36] File name too long` when processing files with very long Japanese filenames. This is due to the Windows filesystem path length limit (MAX_PATH = 260 characters).

### Example Error
```
OSError: [Errno 36] File name too long:
'/mnt/e/DVDFab/StreamFab/Output/TVer/真夏の怪奇ファイル 2026 見えてしまった…招かれざるモノたち/S01/真夏の怪奇ファイル 2026 見えてしまった…招かれざるモノたち_S01E26081302_最恐の怪奇スポットでカメラが捉えた信じがたい怪現象の数々…真夏の暑さを忘れる恐怖が待ち受ける-translated-balanced.srt'
```

## Solution
Implemented a path length safety system that:

1. **Detects** when output paths would exceed 260 characters
2. **Truncates** filenames intelligently using hash-based naming
3. **Preserves** suffixes (-translated, -balanced, _16k_mono) for file identification
4. **Finds** truncated files flexibly using pattern matching

### Key Components

#### 1. `_safe_output_path(dir_path, base_name, suffix)`
Generates safe output paths that won't exceed OS limits.

**Example:**
- Input: `base_name="真夏の怪奇ファイル 2026...", suffix="-translated"`
- Output: `"C:\path\to\2026_S01E26081302_a1b2c3d4-translated.srt"` (truncated, < 260 chars)

#### 2. `_find_srt_file_flexible(dir_path, base_name, suffix)`
Finds SRT files even when names are truncated, using:
- Exact match first (fast path)
- Pattern matching for truncated files (fallback)

#### 3. Updated Pipeline Integration
- `execute_pipeline_batch()`: Uses safe paths for translated files
- `rebalance_srt_file()`: Uses safe paths for balanced files
- `execute_patcher_batch()`: Uses flexible file finder
- `action_smart_queue()`: Uses flexible file finder

## Testing
Created `test_long_filename.py` to verify the fix:

```bash
python test_long_filename.py
```

**Test Results:**
- ✓ Creates translated files without OSError
- ✓ Creates balanced files without OSError
- ✓ All file paths stay within 260 character limit
- ✓ Files can be found using flexible matching

## Files Modified
- `ShowaSync3.py`: Added `_safe_output_path()` and `_find_srt_file_flexible()` methods
- `test_long_filename.py`: New test file to verify the fix

## How It Works

### Before (Broken)
```python
# This would crash with OSError
output_srt = os.path.join(target_dir, f"{base_name}-translated.srt")
# base_name = "真夏の怪奇ファイル 2026 見えてしまった…招かれざるモノたち_S01E26081302_最恐の怪奇スポットでカメラが捉えた信じがたい怪現象の数々…真夏の暑さを忘れる恐怖が待ち受ける"
# Result: 350+ character path → OSError!
```

### After (Fixed)
```python
# Safe path generation with automatic truncation
output_srt = self._safe_output_path(target_dir, base_name, "-translated")
# Result: "<260 char path with hash suffix> → No OSError!
```

## Benefits
1. **No more crashes**: Files with long Japanese names now process successfully
2. **Backward compatible**: Short filenames work exactly as before
3. **Transparent**: Users don't need to change anything
4. **Maintainable**: Centralized path safety logic in helper methods

## Notes
- The truncation uses MD5 hashes (8 chars) to ensure uniqueness
- Filenames are truncated to ~200 chars maximum (leaving room for directory path)
- The system logs when truncation occurs for debugging
- All downstream code uses flexible file finding to locate truncated files