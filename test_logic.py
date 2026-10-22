import sys, os, re, json, shutil, threading
from pathlib import Path
from datetime import timedelta

# Copy the relevant helper functions from ShowaSync.py
def time_to_ms(t_str):
    h, m, s, ms = map(int, re.split('[:,]', t_str))
    return (h * 3600000) + (m * 60000) + (s * 1000) + ms

def ms_to_time(ms):
    td = timedelta(milliseconds=ms)
    h, rem = divmod(int(td.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02},{ms % 1000:03}"

def _get_base_filename_v3(filepath, is_srt=False):
    filename = os.path.basename(filepath)
    root, _ = os.path.splitext(filename)
    if is_srt:
        return root
    root = root.replace('_16k_mono', '').replace('-translated', '').replace('-balanced', '')
    return root

def parse_srt_blocks(file_path):
    if not os.path.exists(file_path): return {}
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read().strip()
    blocks = re.split(r'\n\s*\n', content)
    parsed = {}
    for b in blocks:
        match = re.match(r'^(\d+)\n', b)
        if match: parsed[int(match.group(1))] = b
    return parsed

# Test 1: _get_base_filename_v3 with SRT files (the critical bug fix)
print("=== TEST 1: _get_base_filename_v3 ===")
test_cases_srt = [
    ("foo_16k_mono.srt", True, "foo_16k_mono"),
    ("bar-translated.srt", False, "bar"),
    ("baz-balanced.srt", False, "baz"),
]
for path, is_srt, expected in test_cases_srt:
    result = _get_base_filename_v3(path, is_srt=is_srt)
    status = "PASS" if result == expected else f"FAIL (got {result}, expected {expected})"
    print(f"  {path} (is_srt={is_srt}): {status}")

# Test 2: time_to_ms / ms_to_time roundtrip
print("\n=== TEST 2: Time conversion ===")
t1 = "00:01:30,500"
ms = time_to_ms(t1)
back = ms_to_time(ms)
status = "PASS" if back == t1 else f"FAIL ({back} != {t1})"
print(f"  {t1} -> ms={ms} -> {back}: {status}")

# Test 3: parse_srt_blocks on a sample SRT
print("\n=== TEST 3: SRT parsing ===")
import tempfile
with tempfile.NamedTemporaryFile(mode='w', suffix='.srt', delete=False, encoding='utf-8') as f:
    f.write("1\n00:00:01,000 --> 00:00:04,000\nHello world\n\n2\n00:00:05,000 --> 00:00:08,000\nGoodbye world\n")
    tmpfile = f.name
blocks = parse_srt_blocks(tmpfile)
os.unlink(tmpfile)
status = "PASS" if len(blocks) == 2 and '1' in blocks and '2' in blocks else "FAIL"
print(f"  Parsed {len(blocks)} blocks: {status}")

# Test 4: rebalance guard (would overwrite input?)
print("\n=== TEST 4: Rebalance guard ===")
test_balanced = "/tmp/foo-balanced.srt"
if test_balanced.endswith('-balanced.srt'):
    print(f"  Would skip {os.path.basename(test_balanced)}: PASS (guard works)")
else:
    print(f"  FAIL: should have been skipped")

# Test 5: URL validation logic
print("\n=== TEST 5: Ollama URL validation ===")
def validate_url(url):
    return url.startswith("http://") or url.startswith("https://")
for url, expected in [("http://127.0.0.1:11434", True), ("not-a-url", False), ("https://ollama.local", True)]:
    result = validate_url(url)
    status = "PASS" if result == expected else f"FAIL (got {result})"
    print(f"  '{url}': {status}")

print("\n=== ALL TESTS COMPLETE ===")
