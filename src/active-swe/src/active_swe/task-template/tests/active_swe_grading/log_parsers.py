"""Multi-language test output parsers for Active-SWE.

Ported from build_f2p_p2p_xp.py. Parses per-test pass/fail status
from test framework output logs.

Each parser function takes a string (test output) and returns
Dict[str, str] mapping test_name -> status ('PASSED', 'FAILED', 'ERROR', 'SKIPPED', etc.)
"""

import functools
import json
import logging
import re
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Comprehensive ANSI escape sequence stripper — covers:
# - CSI sequences: \x1b[...<letter>  (colors, cursor, erase, scroll, etc.)
# - OSC sequences: \x1b]...<BEL or ST>
# - charset designations: \x1b(,+,-,./ and \x1b[AB012
# - other 2-byte ESC sequences: \x1b<letter>
_ANSI_RE = re.compile(
    r'\x1b\[[0-9;]*[A-Za-z]'      # CSI: \x1b[<params><letter>
    r'|\x1b\][^\x07]*\x07'         # OSC: \x1b]...BEL
    r'|\x1b\][^\x1b]*\x1b\\'      # OSC: \x1b]...ST (\x1b\\)
    r'|\x1b[()\ +-/."0-9AB]'      # Charset/designation
    r'|\x1b[^[\]()]'              # Other 2-byte ESC sequences
    r'|\[[0-9;]*m'                 # Bare [..m without \x1b (already stripped)
)

def strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from text."""
    return _ANSI_RE.sub('', text)


def _strip_ansi_input(func: Callable) -> Callable:
    """Decorator that applies strip_ansi to the 'output' argument before calling the parser.
    Most parsers open with `output = strip_ansi(output)` or `clean_output = output`.
    This decorator handles that automatically, so the parser function receives already-stripped input.
    """
    @functools.wraps(func)
    def wrapper(output: str, *args, **kwargs) -> Dict[str, str]:
        return func(strip_ansi(output), *args, **kwargs)
    return wrapper


# Unicode pass/fail symbols used by JS test runners (Jest, Vitest, Mocha, AVA, Karma)
_PASS_SYMBOLS = ('✓', '√', '✔', '✅')
_FAIL_SYMBOLS = ('✕', '✗', '×', '✖', '❌')


def _extract_symbol_test_name(line: str, validator: Callable[[str], bool] = None) -> Optional[Tuple[str, str]]:
    """Extract test name and status from a line containing a Unicode pass/fail symbol.

    Returns (status, test_name) or None if no symbol found.
    Status is 'PASSED' for checkmark symbols, 'FAILED' for cross symbols.
    The validator, if provided, is called with the extracted name; if it returns False, None is returned.
    """
    for sym in _PASS_SYMBOLS:
        if sym in line:
            name = normalize_jest_test_name(line[line.index(sym) + len(sym):].strip())
            if name and len(name) > 1 and (validator is None or validator(name)):
                return ('PASSED', name)
            return None
    for sym in _FAIL_SYMBOLS:
        if sym in line:
            name = normalize_jest_test_name(line[line.index(sym) + len(sym):].strip())
            if name and len(name) > 1 and (validator is None or validator(name)):
                return ('FAILED', name)
            return None
    return None


def normalize_jest_test_name(test_name: str) -> str:
    """Normalize jest test name."""
    test_name = re.sub(r'\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\)$', '', test_name)
    return test_name.strip()


@_strip_ansi_input
def parse_pytest_output(output: str) -> Dict[str, str]:
    """Parse pytest test output."""
    results = {}
    
    clean_output = strip_ansi(output)

    
    final_failed = 0
    final_passed = 0
    final_error = 0
    final_skipped = 0
    final_xfailed = 0

    
    summary_match = re.search(r'=\s*(\d+)\s+failed\s*,\s*(\d+)\s+passed', clean_output)
    if summary_match:
        final_failed = int(summary_match.group(1))
        final_passed = int(summary_match.group(2))
    
    if not summary_match:
        summary_match = re.search(r'(\d+)\s+errors?\s*,\s*(\d+)\s+passed', clean_output, re.IGNORECASE)
        if summary_match:
            final_error = int(summary_match.group(1))
            final_passed = int(summary_match.group(2))
            final_failed = final_error
    
    if not summary_match and final_error == 0:
        summary_match = re.search(r'=\s*(\d+)\s+errors?\s+in\s+[\d.]+s\s*=', clean_output, re.IGNORECASE)
        if summary_match:
            final_error = int(summary_match.group(1))
            final_failed = final_error
    
    if not summary_match:
        summary_match = re.search(r'(\d+)\s+failed\s*,\s*(\d+)\s+passed', clean_output)
        if summary_match:
            final_failed = int(summary_match.group(1))
            final_passed = int(summary_match.group(2))

    merged_output = re.sub(r' (\w{3,5})\n(\w{2,4})\s', lambda m: f' {m.group(1)}{m.group(2)} ' if m.group(1)+m.group(2) in ('PASSED', 'FAILED', 'ERROR', 'SKIPPED', 'XFAIL', 'XPASS') else m.group(0), clean_output)
    lines = merged_output.split('\n')
    in_failures_section = False

    
    progress_failed = 0
    progress_passed = 0
    progress_error = 0
    progress_xfailed = 0
    progress_skipped = 0

    
    xdist_prefix = re.compile(r'^\[gw\d+\]\s*')

    for line in lines:
        line_stripped = line.strip()
        
        xdist_match = xdist_prefix.match(line_stripped)
        if xdist_match:
            line_stripped = line_stripped[xdist_match.end():]
        
        pct_match = re.match(r'^\[\s*\d+%\]\s*', line_stripped)
        if pct_match:
            line_stripped = line_stripped[pct_match.end():]

        if 'FAILURES' in line and '=' in line:
            in_failures_section = True
            continue
        if in_failures_section and line.startswith('===') and 'FAILURES' not in line:
            in_failures_section = False
            continue
        if in_failures_section and line_stripped.startswith('_'):
            match = re.match(r'^_+\s*(.+?)\s*_+$', line_stripped)
            if match:
                extracted = match.group(1).strip()
                # Skip if extracted is just underscores/spaces (pytest separator lines)
                # or if we already have this test with :: path (avoid duplicates)
                if extracted and not re.match(r'^[_\s]+$', extracted):
                    # Skip short names when we already have the full :: path
                    already_have_full_path = any(
                        k.endswith('::' + extracted) for k in results
                    )
                    if not already_have_full_path:
                        results[extracted] = 'FAILED'
                continue

        if '::' in line_stripped:
            match = re.match(r'^(.+?::\w+\[.+?\])\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)', line_stripped)
            if match:
                results[match.group(1)] = match.group(2)
                continue
            match = re.match(r'^(.+?::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)', line_stripped)
            if match:
                results[match.group(1)] = match.group(2)
                continue

        if line_stripped.startswith(('PASSED ', 'FAILED ', 'ERROR ', 'SKIPPED ', 'XFAIL ', 'XPASS ')):
            parts = line_stripped.split(None, 1)
            if len(parts) >= 2:
                test_name = parts[1].split()[0]
                if '::' in test_name or test_name.endswith('.py'):
                    results[test_name] = parts[0]

        
        trailing_match = re.match(r'^(.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)', line_stripped)
        if trailing_match:
            test_name = trailing_match.group(1).strip()
            status = trailing_match.group(2)
            if '::' in test_name or test_name.endswith('.py') or test_name.startswith('test_'):
                results[test_name] = status

        
        # . = passed, F = failed, E = error, x/X = xfailed, s/S = skipped
        progress_match = re.match(r'^(.+?\.py)\s+([.FxXsSE]+)\s+\[', line_stripped)
        if progress_match:
            test_chars = progress_match.group(2)
            progress_passed += test_chars.count('.')
            progress_failed += test_chars.count('F')
            progress_error += test_chars.count('E')
            progress_xfailed += test_chars.count('x') + test_chars.count('X')
            progress_skipped += test_chars.count('s') + test_chars.count('S')
            continue

        
        if line_stripped.startswith('ERROR: ') and ' - ' in line_stripped:
            parts = line_stripped.split(' - ', 1)
            if len(parts) >= 1:
                test_name = parts[0].replace('ERROR: ', '').strip()
                results[test_name] = 'ERROR'

        
        file_error_match = re.match(r'^ERROR collecting ([^\s]+\.py)', line_stripped)
        if file_error_match:
            results[file_error_match.group(1)] = 'ERROR'

        if line_stripped.startswith('ModuleNotFoundError:') or line_stripped.startswith('SyntaxError:'):
            file_match = re.search(r'([^\s]+\.py)', line_stripped)
            if file_match:
                results[file_match.group(1)] = 'ERROR'

    
    parsed_failed = len([v for v in results.values() if v == 'FAILED'])
    parsed_passed = len([v for v in results.values() if v == 'PASSED'])

    
    if progress_failed > 0 or progress_passed > 0:
        if parsed_failed == 0 and parsed_passed == 0:
            
            if progress_failed > 0:
                results[f'_pytest_progress_{progress_failed}_failed_'] = 'FAILED'
            if progress_passed > 0:
                results[f'_pytest_progress_{progress_passed}_passed_'] = 'PASSED'
        elif final_failed > 0 and (parsed_failed != final_failed or parsed_passed != final_passed):
            
            results['_pytest_summary_failed_'] = str(final_failed)
            results['_pytest_summary_passed_'] = str(final_passed)

    
    if len(results) == 0:
        summary_match = re.search(r'(\d+)\s+failed', clean_output, re.IGNORECASE)
        errors_match = re.search(r'(\d+)\s+errors?', clean_output, re.IGNORECASE)
        total_failures = 0
        if summary_match:
            total_failures += int(summary_match.group(1))
        if errors_match:
            total_failures += int(errors_match.group(1))
        if total_failures > 0:
            results['_pytest_summary_failed_'] = 'FAILED'

    # Remove file-level entries (.py without ::) when case-level entries (::) exist
    # for the same file. File-level entries like "tests/test_foo.py FAILED" are
    # redundant noise when we also have "tests/test_foo.py::TestClass::test_bar FAILED".
    file_level_keys = set()
    case_level_files = set()  # files that have case-level (::) entries
    for key in results:
        if '::' in key:
            # Extract file path from case-level entry (e.g. "tests/test_foo.py::TestClass::test_bar")
            file_part = key.split('::')[0]
            case_level_files.add(file_part)
        elif key.endswith('.py') and '/' in key and not key.startswith('_'):
            file_level_keys.add(key)
    for fl_key in file_level_keys:
        if fl_key in case_level_files:
            del results[fl_key]

    return results


def parse_unittest_output(output: str) -> Dict[str, str]:
    """Parse unittest test output."""
    _VERBOSE_LINE1 = re.compile(r'^(test_\w+)\s*\((.+)\)\s*$')
    _IS_TEST_NAME = re.compile(r'^test_\w+\s*\(')

    results = {}
    lines = output.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Try two-line verbose format first:
        # Line N:   test_func_name (module.Class)
        # Line N+1: docstring ... ok/FAIL/ERROR
        m1 = _VERBOSE_LINE1.match(line)
        if m1 and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if ' ... ' in next_line:
                parts = next_line.rsplit(' ... ', 1)
                if len(parts) == 2:
                    docstring_part = parts[0].strip()
                    # Guard: if docstring_part looks like another test name,
                    # this is actually single-line verbose, not two-line docstring
                    if not _IS_TEST_NAME.match(docstring_part):
                        result = parts[1].strip().upper()
                        test_name = m1.group(1)  # Use real test name from line 1
                        if result in ('OK', 'PASS', 'PASSED'):
                            results[test_name] = 'PASSED'
                        elif result in ('FAIL', 'FAILED'):
                            results[test_name] = 'FAILED'
                        elif result == 'ERROR':
                            results[test_name] = 'ERROR'
                        i += 2  # Skip both lines
                        continue

        # Single-line format: test_name ... ok/FAIL/ERROR
        # Also handles: test_name (module.Class) ... ok/FAIL/ERROR
        # Skip _FailedTest entries (import errors, not real tests)
        if ' ... ' in line:
            parts = line.rsplit(' ... ', 1)
            if len(parts) == 2:
                test_name, result = parts[0].strip(), parts[1].strip().upper()
                if '_FailedTest' in test_name:
                    pass  # Not a real test — module failed to import
                elif result in ('OK', 'PASS', 'PASSED'):
                    results[test_name] = 'PASSED'
                elif result in ('FAIL', 'FAILED'):
                    results[test_name] = 'FAILED'
                elif result == 'ERROR':
                    results[test_name] = 'ERROR'

        # FAIL:/ERROR: summary lines — strip (module.Class) suffix
        # Skip _FailedTest entries (import errors, not real tests)
        if line.startswith('FAIL: ') or line.startswith('ERROR: '):
            parts = line.split(None, 1)
            if len(parts) >= 2:
                test_name = parts[1].strip()
                if '_FailedTest' in test_name:
                    pass  # Not a real test
                else:
                    m_fc = re.match(r'^(test_\w+)\s*\(.+\)$', test_name)
                    if m_fc:
                        test_name = m_fc.group(1)
                    results[test_name] = 'FAILED' if line.startswith('FAIL:') else 'ERROR'

        i += 1

    # Strip parameterized suffix from test names: test_foo(key=val) -> test_foo
    # This is done in a post-pass to handle both "test ... ok" and "FAIL: test" formats
    cleaned = {}
    for name, status in results.items():
        cleaned_name = _strip_param_suffix(name)
        # Use cleaned name, but don't overwrite if collision
        if cleaned_name not in cleaned:
            cleaned[cleaned_name] = status
        else:
            # If collision, keep FAILED/ERROR priority
            if status in ('FAILED', 'ERROR') and cleaned[cleaned_name] == 'PASSED':
                cleaned[cleaned_name] = status
    return cleaned


def parse_nosetests_output(output: str) -> Dict[str, str]:
    """Parse nosetests test output."""
    # First try parse_unittest_output which handles two-line verbose format correctly.
    # nosetests and python -m unittest -v can produce similar output.
    unittest_results = parse_unittest_output(output)
    if unittest_results and not any(k.startswith('_nosetests_') for k in unittest_results):
        # parse_unittest_output found real test names — use them
        return unittest_results

    results = {}
    output = output.strip()

    
    
    has_dots = '....' in output or re.search(r'\.{5,}', output)

    
    if re.search(r'Ran\s+(\d+)\s+tests?', output, re.IGNORECASE):
        
        ok_patterns = [
            re.search(r'\n+OK\n', output),
            re.search(r'\nOK\s*\(', output),
            re.search(r'\n+OK$', output),
            re.search(r'\nOK\b', output),  # OK followed by word boundary (e.g. OK\nMore text)
        ]
        if any(ok_patterns):
            
            for line in output.split('\n'):
                line = line.strip()
                if ' ... ' in line:
                    parts = line.rsplit(' ... ', 1)
                    if len(parts) == 2:
                        test_name, result = parts[0].strip(), parts[1].strip().upper()
                        if '_FailedTest' in test_name:
                            pass  # Import error, not a real test
                        elif result in ('OK', 'PASS', 'PASSED'):
                            results[test_name] = 'PASSED'
                        elif result in ('FAIL', 'FAILED'):
                            results[test_name] = 'FAILED'
            if not results:
                
                results['_nosetests_all_passed_'] = 'PASSED'
        elif re.search(r'\n+FAILED', output):
            
            for line in output.split('\n'):
                line = line.strip()
                if ' ... ' in line:
                    parts = line.rsplit(' ... ', 1)
                    if len(parts) == 2:
                        test_name, result = parts[0].strip(), parts[1].strip().upper()
                        if '_FailedTest' in test_name:
                            pass  # Import error, not a real test
                        elif result in ('OK', 'PASS', 'PASSED'):
                            results[test_name] = 'PASSED'
                        elif result in ('FAIL', 'FAILED'):
                            results[test_name] = 'FAILED'
                        elif result == 'ERROR':
                            results[test_name] = 'ERROR'
                
                if line.startswith('FAIL: ') or line.startswith('ERROR: '):
                    status = 'FAILED' if line.startswith('FAIL:') else 'ERROR'
                    if '_FailedTest' in line:
                        pass  # Import error, not a real test
                    else:
                        m = re.match(r'^(FAIL|ERROR):\s+(\S+)', line)
                        if m:
                            test_name = m.group(2)
                            results[test_name] = status
                
                m = re.match(r'^_{3,}\s*(.+?)\s*_{3,}$', line)
                if m:
                    ud_name = m.group(1).strip()
                    if '_FailedTest' not in ud_name:
                        results[ud_name] = 'FAILED'
            if not results:
                results['_nosetests_failed_'] = 'FAILED'

    # If unittest_results found something but nosetests also found something,
    # prefer unittest_results (which uses real test names)
    if unittest_results:
        return unittest_results

    
    elif has_dots and 'Ran' not in output:
        
        dot_match = re.findall(r'\.+', output)
        if dot_match:
            test_count = sum(len(d) for d in dot_match)
            if test_count > 0:
                results['_nosetests_dot_format_'] = f'{test_count}_PASSED'

    # Strip parameterized suffix from test names: test_foo(key=val) -> test_foo
    cleaned = {}
    for name, status in results.items():
        cleaned_name = _strip_param_suffix(name)
        if cleaned_name not in cleaned:
            cleaned[cleaned_name] = status
        else:
            if status in ('FAILED', 'ERROR') and cleaned[cleaned_name] == 'PASSED':
                cleaned[cleaned_name] = status
    return cleaned


def normalize_test_name(test_name: str) -> str:
    """Normalize test name."""
    
    normalized = re.sub(r'\[.+?\]', '', test_name)

    
    normalized = re.sub(r'\.param_\d+(\.|$)', r'\1', normalized)

    
    if '::' in normalized:
        parts = normalized.split('::')
        # Rust: crate::module::test_name -> test_name (last segment)
        if not normalized.endswith('.py') and not any('.' in p for p in parts[:-1]):
            normalized = parts[-1]
        elif len(parts) >= 2:
            last_parts = parts[-2:]
            normalized = '.'.join(last_parts)

    
    normalized = re.sub(r'\.(py|go|js|ts|rb|rs|java|php|c|cpp|cc|cxx|h|hpp)$', '', normalized)

    # 4. Go: github.com/user/repo/pkg.TestName -> TestName (strip package path)
    if '/' in normalized and '.' in normalized:
        go_match = re.match(r'[\w/\.\-]+\.(\w+)(?:/(\w+))?$', normalized)
        if go_match:
            normalized = go_match.group(2) if go_match.group(2) else go_match.group(1)

    # 5. Java: com.example.Class.method -> Class.method (3+ dot segments)
    if normalized.count('.') >= 2 and '/' not in normalized:
        parts = normalized.split('.')
        if (all(p and p[0].islower() for p in parts[:-2])
                and parts[-1] and parts[-1][0].islower()
                and len(parts) >= 3):
            # Looks like com.example.Class.method -> Class.method
            normalized = '.'.join(parts[-2:])

    # 6. Strip trailing whitespace then trailing dots, normalize whitespace
    normalized = normalized.strip().rstrip('.').strip()
    normalized = re.sub(r'\s+', ' ', normalized)

    if not normalized:
        return test_name
    return normalized


def create_test_name_mapping(tests: Dict[str, str]) -> Dict[str, str]:
    """Create test name mapping."""
    mapping = {}
    for test_name, status in tests.items():
        
        mapping[test_name] = status
        
        normalized = normalize_test_name(test_name)
        if normalized != test_name:
            mapping[normalized] = status
    return mapping


@_strip_ansi_input
def parse_go_test_output(output: str) -> Dict[str, str]:
    """Parse go test test output."""
    
    clean_output = strip_ansi(output)

    
    if ('Ginkgo ran' in clean_output or
        re.search(r'FAIL!\s*--\s*\d+\s+Passed.*\d+\s+Failed', clean_output, re.IGNORECASE) or
        re.search(r'SUCCESS!\s*--\s*\d+\s+Passed', clean_output, re.IGNORECASE) or
        re.search(r'Ran\s+\d+\s+of\s+\d+\s+Specs?', clean_output)):
        return parse_ginkgo_output(output)

    results = {}
    output = clean_output

    
    
    all_build_failed = False
    if ('[build failed]' in output or '[compile failed]' in output):
        has_normal_test = bool(re.search(r'--- (PASS|FAIL):', output)) or bool(re.search(r'ok\s+[\w/\.\-]+\s+\d', output))
        if not has_normal_test:
            # Record build-failed packages as ERROR so parse_f2p_p2p can match them
            for m in re.finditer(r'FAIL\s+([\w/\.\-]+)\s+\[build failed\]', output):
                results[m.group(1)] = 'ERROR'
            for m in re.finditer(r'FAIL\s+([\w/\.\-]+)\s+\[compile failed\]', output):
                results[m.group(1)] = 'ERROR'
            if results:
                return results
            return {}

    
    if output.strip().startswith('{') and '"Action"' in output:
        import json
        passed_count = 0
        failed_count = 0
        for line in output.strip().split('\n'):
            try:
                if line.strip():
                    data = json.loads(line)
                    action = data.get('Action', '')
                    test_name = data.get('Test', '')
                    if action == 'pass' and test_name:
                        
                        if '/' not in test_name:
                            results[test_name] = 'PASSED'
                        passed_count += 1
                    elif action == 'fail' and test_name:
                        if '/' not in test_name:
                            results[test_name] = 'FAILED'
                        failed_count += 1
                    elif action == 'skip' and test_name:
                        if '/' not in test_name:
                            results[test_name] = 'SKIPPED'
            except json.JSONDecodeError:
                continue

        
        if len(results) == 0:
            if passed_count > 0:
                results['_go_test_json_passed_'] = str(passed_count)
            if failed_count > 0:
                results['_go_test_json_failed_'] = str(failed_count)
        return results

    
    summary_match = re.search(r'(\d+)\s+passed[,\s]+(\d+)\s+failed', output, re.IGNORECASE)
    if summary_match:
        passed = int(summary_match.group(1))
        failed = int(summary_match.group(2))
        if failed > 0:
            results['_go_test_summary_failed_'] = str(failed)
        if passed > 0:
            results['_go_test_summary_passed_'] = str(passed)
        return results

    
    current_test = None

    for line in output.split('\n'):
        line_stripped = line.strip()

        
        match = re.match(r'^--- (PASS|FAIL|SKIP):\s+(\S+)(?:\s|$)', line_stripped)
        if match:
            test_name = match.group(2)
            
            if '/' not in test_name:
                results[test_name] = match.group(1) + 'ED'
            current_test = None
            continue

        
        run_match = re.match(r'^=== RUN\s+(\S+)', line_stripped)
        if run_match:
            current_test = run_match.group(1)
            continue

        
        
        
        pkg_match = re.match(r'^(PASS|FAIL|ok)\s+([\w/\.\-]+(?:/[\w/\.\-]+)*)(?:\s+|\s*$)', line_stripped)
        if pkg_match:
            pkg_name = pkg_match.group(2)
            status_word = pkg_match.group(1).upper()
            
            if '[build failed]' in line_stripped or '[compile failed]' in line_stripped:
                results[f'_go_build_failed_{pkg_name}'] = 'ERROR'
                continue
            
            
            is_pkg = ('/' in pkg_name or pkg_name.startswith('.') or pkg_name == pkg_name.lower())
            if is_pkg and not pkg_name.startswith('Test'):
                if status_word == 'OK':
                    # ok = PASSED at package level
                    if pkg_name not in results:
                        results[pkg_name] = 'PASSED'
                elif status_word in ('PASS', 'FAIL'):
                    status = status_word + 'ED'
                    if status == 'FAILED':
                        
                        results[f'_go_pkg_failed_{pkg_name}'] = 'FAILED'
                    elif pkg_name not in results:
                        results[pkg_name] = status
            continue

    
    
    fail_summary = re.search(r'FAIL\s+[\w/\.\-]+(?:\s+\S+)?\s+(\d+\.\d+s|\[build failed\])', output)
    if fail_summary:
        
        has_failed = any(v in ('FAILED', 'ERROR') for v in results.values())
        if not has_failed:
            count_match = re.search(r'(\d+)\s+failed', output, re.IGNORECASE)
            if count_match:
                results['_go_test_failed_'] = 'FAILED'

    # OOPS/OK summary format: "OOPS: 21 passed, 1 FAILED" or "OK: 22 passed"
    oops_match = re.search(r'OOPS:\s*(\d+)\s+passed(?:,\s*(\d+)\s+FAILED)?', output, re.IGNORECASE)
    if oops_match:
        passed = int(oops_match.group(1))
        failed = int(oops_match.group(2) or 0)
        if failed > 0 and not any(v == 'FAILED' for v in results.values()):
            results['_go_oops_failed_'] = 'FAILED'
        if passed > 0 and not any(v == 'PASSED' for v in results.values()):
            results['_go_oops_passed_'] = 'PASSED'
    ok_match = re.search(r'^OK:\s*(\d+)\s+passed', output, re.MULTILINE | re.IGNORECASE)
    if ok_match:
        passed = int(ok_match.group(1))
        if passed > 0 and not any(v == 'PASSED' for v in results.values()):
            results['_go_ok_passed_'] = 'PASSED'

    return results


def _is_jest_file_path(name: str) -> bool:
    """Check if a name looks like a file path (not a test case name)."""
    return bool('/' in name and re.search(r'\.\w+$', name))


def _close_jest_file_context(results: dict, current_file: str, current_file_status: str,
                              current_file_has_tests: bool, file_test_map: dict):
    """Close the current file context in Jest/Vitest parser.

    When a file section ends without individual test results (e.g. "Test suite failed to run"),
    emit a suite-level marker so parse_f2p_p2p can expand it later using after-log test names.
    When individual tests were found, the file-level entry is skipped (case-level is sufficient).
    """
    if current_file is None:
        return
    if not current_file_has_tests:
        if current_file_status == 'FAIL':
            results[f'__suite_fail__:{current_file}'] = 'FAILED'
        elif current_file_status == 'PASS':
            results[f'__suite_pass__:{current_file}'] = 'PASSED'


@_strip_ansi_input
def parse_jest_output(output: str) -> Dict[str, str]:
    """Parse jest test output."""
    results = {}

    
    
    clean_output = strip_ansi(output)

    
    final_failed = 0
    final_passed = 0
    final_match = re.search(r'Tests:\s*(\d+)\s+failed\s*,\s*(\d+)\s+passed', clean_output)
    if final_match:
        final_failed = int(final_match.group(1))
        final_passed = int(final_match.group(2))

    # File context tracking
    current_file = None          
    current_file_status = None   # 'PASS' or 'FAIL'
    current_file_has_tests = False  
    file_test_map = {}           # filepath -> [test_name, ...]

    for line in clean_output.split('\n'):
        line_stripped = line.strip()

        
        # Note: Jest sometimes appends timing like "PASS file.ts (7.946 s)"
        match = re.match(r'^(PASS|FAIL)\s+(.+\.js|.+\.ts|.+\.jsx|.+\.tsx)(?:\s+\([^)]+\))?\s*$', line_stripped)
        if match:
            
            _close_jest_file_context(results, current_file, current_file_status,
                                     current_file_has_tests, file_test_map)
            
            filepath = match.group(2).strip()
            current_file = filepath
            current_file_status = match.group(1)
            current_file_has_tests = False
            file_test_map.setdefault(current_file, [])
            
            continue

        
        fail_test_match = re.match(r'^FAIL\s+test\s+(.+\.js|.+\.ts|.+\.jsx|.+\.tsx)(?:\s+\([^)]+\))?\s*$', line_stripped)
        if fail_test_match:
            
            _close_jest_file_context(results, current_file, current_file_status,
                                     current_file_has_tests, file_test_map)
            filepath = fail_test_match.group(1).strip()
            current_file = filepath
            current_file_status = 'FAIL'
            current_file_has_tests = False
            file_test_map.setdefault(current_file, [])
            continue

        
        pass_test_match = re.match(r'^PASS\s+test\s+(.+\.js|.+\.ts|.+\.jsx|.+\.tsx)(?:\s+\([^)]+\))?\s*$', line_stripped)
        if pass_test_match:
            _close_jest_file_context(results, current_file, current_file_status,
                                     current_file_has_tests, file_test_map)
            filepath = pass_test_match.group(1).strip()
            current_file = filepath
            current_file_status = 'PASS'
            current_file_has_tests = False
            file_test_map.setdefault(current_file, [])
            continue

        
        if '✓' in line_stripped or '√' in line_stripped:
            
            for sym in ['✓', '√', '✅']:
                if sym in line_stripped:
                    idx = line_stripped.index(sym)
                    test_name = line_stripped[idx + 1:].strip()
                    test_name = normalize_jest_test_name(test_name)
                    if test_name and is_valid_test_name(test_name):
                        results[test_name] = 'PASSED'
                        current_file_has_tests = True
                        if current_file:
                            file_test_map.setdefault(current_file, []).append(test_name)
                    break
            continue

        
        if '✕' in line_stripped or '✗' in line_stripped or '×' in line_stripped:
            for sym in ['✕', '✗', '×', '❌']:
                if sym in line_stripped:
                    idx = line_stripped.index(sym)
                    test_name = line_stripped[idx + 1:].strip()
                    test_name = normalize_jest_test_name(test_name)
                    if test_name and is_valid_test_name(test_name):
                        results[test_name] = 'FAILED'
                        current_file_has_tests = True
                        if current_file:
                            file_test_map.setdefault(current_file, []).append(test_name)
                    break
            continue

        
        
        if '●' in line_stripped:
            idx = line_stripped.index('●')
            test_name = line_stripped[idx + 1:].strip()
            
            test_name = re.sub(r'\s*\(\d+\s*(?:ms|s)\)$', '', test_name)
            
            if test_name and test_name.lower() == 'test suite failed to run':
                continue
            if test_name and len(test_name) > 2 and is_valid_test_name(test_name):
                results[test_name] = 'FAILED'
                current_file_has_tests = True
                if current_file:
                    file_test_map.setdefault(current_file, []).append(test_name)
            continue

    
    _close_jest_file_context(results, current_file, current_file_status,
                             current_file_has_tests, file_test_map)

    
    parsed_failed = len([v for v in results.values() if v == 'FAILED'])
    parsed_passed = len([v for v in results.values() if v == 'PASSED'])

    
    if final_failed > 0 and parsed_failed != final_failed:
        
        results[f'_jest_summary_{final_failed}_failed_'] = 'FAILED'
    if final_passed > 0 and parsed_passed != final_passed:
        results[f'_jest_summary_{final_passed}_passed_'] = 'PASSED'

    
    # Tests: X failed, Y passed, Z total
    if len(results) == 0:
        failed_match = re.search(r'(\d+)\s+failed', clean_output, re.IGNORECASE)
        if failed_match and int(failed_match.group(1)) > 0:
            results['_jest_tests_failed_'] = 'FAILED'

    
    if file_test_map:
        results['__file_test_map__'] = json.dumps(file_test_map)

    return results


@_strip_ansi_input
def parse_mocha_output(output: str) -> Dict[str, str]:
    """Parse mocha test output."""
    results = {}

    
    output = strip_ansi(output)

    lines = output.split('\n')
    for i, line in enumerate(lines):
        line_stripped = line.strip()

        
        for sym in ['✔', '✓', '√', '✅']:
            if sym in line_stripped:
                idx = line_stripped.index(sym)
                test_name = line_stripped[idx + 1:].strip()
                
                test_name = re.sub(r'\s*\(\d+\s*(?:ms|s)\)$', '', test_name)
                if test_name and len(test_name) > 1:
                    results[test_name] = 'PASSED'
                break

        
        for sym in ['✖', '×', '❌']:
            if sym in line_stripped:
                idx = line_stripped.index(sym)
                test_name = line_stripped[idx + 1:].strip()
                test_name = re.sub(r'\s*\(\d+\s*(?:ms|s)\)$', '', test_name)
                if test_name and len(test_name) > 1:
                    results[test_name] = 'FAILED'
                break

        
        # Mocha failure detail format:
        #   N) Suite Name
        #        actual test name:
        #        Error message...
        # If the next line is an indented sub-test name, use that instead of suite name.
        match = re.match(r'^(\d+)\)\s+(.+)$', line_stripped)
        if match:
            test_name = match.group(2).strip()
            # Look ahead: if next non-empty line is an indented sub-test name (ends with :),
            # use the sub-test name instead of the suite name
            if i + 1 < len(lines):
                for lookahead in range(i + 1, min(i + 3, len(lines))):
                    next_line = lines[lookahead].strip()
                    if not next_line:
                        continue
                    # Sub-test name like "Finds Ruby when .ruby-version is inside workspace:"
                    # or "mergeComposedEnv merges environment variables:"
                    # Distinguish from error messages: sub-test names end with ':' and
                    # don't start with 'Error' or common error prefixes
                    if (next_line.endswith(':')
                        and not next_line.startswith(('Error', 'TypeError', 'AssertionError', 'ReferenceError', 'RangeError', 'SyntaxError'))
                        and not re.match(r'^(at |->|\.\.\.)', next_line)):
                        test_name = next_line[:-1]  # remove trailing colon
                    break
            if test_name:
                results[test_name] = 'FAILED'
            continue

    
    passing_match = re.search(r'(\d+)\s+passing', output, re.IGNORECASE)
    failing_match = re.search(r'(\d+)\s+failing', output, re.IGNORECASE)
    if passing_match or failing_match:
        
        parsed_passed = len([v for v in results.values() if v == 'PASSED'])
        parsed_failed = len([v for v in results.values() if v == 'FAILED'])
        if passing_match and parsed_passed == 0:
            results['_mocha_tests_passed_'] = 'PASSED'
        if failing_match and int(failing_match.group(1)) > 0 and parsed_failed == 0:
            results['_mocha_tests_failed_'] = 'FAILED'

    return results


@_strip_ansi_input
def parse_karma_output(output: str) -> Dict[str, str]:
    """Parse karma test output."""
    results = {}

    output = strip_ansi(output)

    # Karma progress summary: Executed N of M (X FAILED) or Executed N of M SUCCESS
    # Multiple lines may exist — take the last one (most complete)
    exec_matches = re.findall(
        r'Executed\s+(\d+)\s+of\s+(\d+)\s+\((\d+)\s+FAILED\)',
        output, re.IGNORECASE
    )
    if exec_matches:
        last = exec_matches[-1]
        total = int(last[1])
        failed = int(last[2])
        passed = total - failed
        if failed > 0:
            results['_karma_summary_failed_'] = str(failed)
        if passed > 0:
            results['_karma_summary_passed_'] = str(passed)

    exec_success = re.findall(
        r'Executed\s+(\d+)\s+of\s+(\d+)\s+SUCCESS',
        output, re.IGNORECASE
    )
    if exec_success and not exec_matches:
        last = exec_success[-1]
        total = int(last[1])
        results['_karma_summary_passed_'] = str(total)

    # TOTAL: X FAILED, Y SUCCESS
    total_match = re.search(r'TOTAL:\s*(\d+)\s+FAILED,\s*(\d+)\s+SUCCESS', output, re.IGNORECASE)
    if total_match:
        failed = int(total_match.group(1))
        passed = int(total_match.group(2))
        if failed > 0:
            results['_karma_total_failed_'] = str(failed)
        if passed > 0:
            results['_karma_total_passed_'] = str(passed)

    # Summary counts from various karma reporter formats
    # "X skipped, Y failed, Z successful" or "X tests completed, Y failed, Z successful"
    summary_match = re.search(
        r'(\d+)\s+(?:tests?\s+)?completed[,\s]+(\d+)\s+failed[,\s]+(\d+)\s+successful',
        output, re.IGNORECASE
    )
    if summary_match and not exec_matches and not total_match:
        failed = int(summary_match.group(2))
        passed = int(summary_match.group(3))
        if failed > 0:
            results['_karma_summary_failed_'] = str(failed)
        if passed > 0:
            results['_karma_summary_passed_'] = str(passed)

    # FAILED/PASSED suite names (karma-mocha-reporter, karma-spec-reporter)
    for line in output.split('\n'):
        stripped = line.strip()
        # "FAILED test/some/file.spec.ts" or "PASSED test/some/file.spec.ts"
        suite_match = re.match(r'^(FAILED|PASSED)\s+(\S+\.(?:spec|test)\.\w+)', stripped, re.IGNORECASE)
        if suite_match:
            status = 'FAILED' if suite_match.group(1).upper() == 'FAILED' else 'PASSED'
            results[suite_match.group(2)] = status
            continue

    # Also parse individual test names using mocha-style symbols
    for line in output.split('\n'):
        stripped = line.strip()
        for sym in ['✔', '✓', '√', '✅']:
            if sym in stripped:
                idx = stripped.index(sym)
                test_name = stripped[idx + 1:].strip()
                test_name = re.sub(r'\s*\(\d+\s*(?:ms|s)\)$', '', test_name)
                if test_name and len(test_name) > 1 and is_valid_test_name(test_name):
                    results[test_name] = 'PASSED'
                break
        for sym in ['✖', '×', '✗', '❌']:
            if sym in stripped:
                idx = stripped.index(sym)
                test_name = stripped[idx + 1:].strip()
                test_name = re.sub(r'\s*\(\d+\s*(?:ms|s)\)$', '', test_name)
                if test_name and len(test_name) > 1 and is_valid_test_name(test_name):
                    results[test_name] = 'FAILED'
                break

    # If only summary counts (no named tests), add standardized summary markers
    # so that extract_summary_counts can pick them up
    named_passed = sum(1 for v in results.values() if v == 'PASSED' and not v.startswith('_'))
    named_failed = sum(1 for v in results.values() if v == 'FAILED' and not v.startswith('_'))
    summary_failed = None
    summary_passed = None
    for k, v in results.items():
        if k.startswith('_karma_summary_failed_') or k.startswith('_karma_total_failed_'):
            summary_failed = int(v)
        if k.startswith('_karma_summary_passed_') or k.startswith('_karma_total_passed_'):
            summary_passed = int(v)

    # When summary says some failed but no named tests found, add generic markers
    if summary_failed and summary_failed > 0 and named_failed == 0:
        for i in range(summary_failed):
            results[f'_failed_{i}_'] = 'FAILED'
    if summary_passed and summary_passed > 0 and named_passed == 0:
        for i in range(summary_passed):
            results[f'_passed_{i}_'] = 'PASSED'

    return results


def parse_assemblyscript_output(output: str) -> Dict[str, str]:
    """Parse assemblyscript test output."""
    results = {}
    for line in output.split('\n'):
        line_stripped = line.strip()

        # - test_name SUCCESS
        match = re.match(r'^[-•]\s+(.+?)\s+SUCCESS', line_stripped)
        if match:
            results[match.group(1).strip()] = 'PASSED'
            continue

        # - test_name FAILURE
        match = re.match(r'^[-•]\s+(.+?)\s+FAILURE', line_stripped)
        if match:
            results[match.group(1).strip()] = 'FAILED'
            continue

        # FAILURE: X compiler tests had failures
        failure_match = re.search(r'FAILURE:\s*(\d+)\s+.*tests?\s+had\s+failures?', output, re.IGNORECASE)
        if failure_match and len(results) == 0:
            results['_assemblyscript_tests_failed_'] = 'FAILED'

    return results


@_strip_ansi_input
def parse_ava_output(output: str) -> Dict[str, str]:
    """Parse ava test output."""
    results = {}
    
    
    output = strip_ansi(output)
    
    for line in output.split('\n'):
        line_stripped = line.strip()
        
        
        if '✔' in line_stripped:
            idx = line_stripped.index('✔')
            test_name = line_stripped[idx + 1:].strip()
            
            test_name = re.sub(r'\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\)$', '', test_name)
            if test_name:
                results[test_name] = 'PASSED'
            continue
        
        
        if '✓' in line_stripped:
            idx = line_stripped.index('✓')
            test_name = line_stripped[idx + 1:].strip()
            test_name = re.sub(r'\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\)$', '', test_name)
            if test_name:
                results[test_name] = 'PASSED'
            continue
        
        # ✖ test_name - AVA fail
        if '✖' in line_stripped:
            idx = line_stripped.index('✖')
            test_name = line_stripped[idx + 1:].strip()
            test_name = re.sub(r'\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\)$', '', test_name)
            if test_name:
                results[test_name] = 'FAILED'
            continue
        
        
        if '✕' in line_stripped:
            idx = line_stripped.index('✕')
            test_name = line_stripped[idx + 1:].strip()
            test_name = re.sub(r'\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\)$', '', test_name)
            if test_name:
                results[test_name] = 'FAILED'
            continue
    
    
    passed_match = re.search(r'(\d+)\s+tests?\s+passed', output, re.IGNORECASE)
    failed_match = re.search(r'(\d+)\s+tests?\s+failed', output, re.IGNORECASE)
    
    if failed_match and int(failed_match.group(1)) > 0 and len(results) == 0:
        results['_ava_tests_failed_'] = 'FAILED'
    if passed_match and len(results) == 0:
        results['_ava_tests_passed_'] = 'PASSED'
    
    return results


@_strip_ansi_input
def parse_vitest_output(output: str) -> Dict[str, str]:
    """Parse vitest test output."""
    results = {}

    
    output = strip_ansi(output)

    # File context tracking
    current_file = None
    current_file_status = None
    current_file_has_tests = False
    file_test_map = {}

    
    for line in output.split('\n'):
        line_stripped = line.strip()

        
        fail_match = re.match(r'^FAIL\s+(test/\S+|.+\.\w+)(?:\s+\([^)]+\))?\s*$', line_stripped)
        if fail_match:
            filepath = fail_match.group(1).strip()
            if _is_jest_file_path(filepath) or filepath.startswith('test/'):
                _close_jest_file_context(results, current_file, current_file_status,
                                         current_file_has_tests, file_test_map)
                current_file = filepath
                current_file_status = 'FAIL'
                current_file_has_tests = False
                file_test_map.setdefault(current_file, [])
                continue

        
        pass_match = re.match(r'^PASS\s+(test/\S+|.+\.\w+)(?:\s+\([^)]+\))?\s*$', line_stripped)
        if pass_match:
            filepath = pass_match.group(1).strip()
            if _is_jest_file_path(filepath) or filepath.startswith('test/'):
                _close_jest_file_context(results, current_file, current_file_status,
                                         current_file_has_tests, file_test_map)
                current_file = filepath
                current_file_status = 'PASS'
                current_file_has_tests = False
                file_test_map.setdefault(current_file, [])
                continue

        # ✓ test_name (XXms)  or √ test_name  (some terminals use √ instead of ✓)
        for sym in ['✓', '√']:
            if sym in line_stripped:
                idx = line_stripped.index(sym)
                test_name = line_stripped[idx + 1:].strip()
                
                test_name = re.sub(r'\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\)$', '', test_name)
                if test_name:
                    results[test_name] = 'PASSED'
                    current_file_has_tests = True
                    if current_file:
                        file_test_map.setdefault(current_file, []).append(test_name)
                break
        else:
            # × test_name (failed)
            for sym in ['×', '✗', '✕']:
                if sym in line_stripped:
                    idx = line_stripped.index(sym)
                    test_name = line_stripped[idx + 1:].strip()
                    test_name = re.sub(r'\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\)$', '', test_name)
                    if test_name:
                        results[test_name] = 'FAILED'
                        current_file_has_tests = True
                        if current_file:
                            file_test_map.setdefault(current_file, []).append(test_name)
                    break
            else:
                # ❯ test_name (failure marker in vitest)
                if line_stripped.startswith('❯'):
                    test_name = line_stripped[1:].strip()
                    test_name = re.sub(r'\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\)$', '', test_name)
                    if test_name and not test_name.startswith('test'):
                        results[test_name] = 'FAILED'
                        current_file_has_tests = True
                        if current_file:
                            file_test_map.setdefault(current_file, []).append(test_name)

    
    _close_jest_file_context(results, current_file, current_file_status,
                             current_file_has_tests, file_test_map)

    
    files_match = re.search(r'Test Files\s+(\d+)\s+failed.*?(\d+)\s+passed', output, re.IGNORECASE)
    if files_match:
        failed = int(files_match.group(1))
        passed = int(files_match.group(2))
        if failed > 0:
            results['_vitest_files_failed_'] = 'FAILED'
        if passed > 0:
            results['_vitest_files_passed_'] = 'PASSED'

    # Tests X failed | Y passed
    tests_match = re.search(r'Tests\s+(\d+)\s+failed.*?(\d+)\s+passed', output, re.IGNORECASE)
    if tests_match:
        failed = int(tests_match.group(1))
        passed = int(tests_match.group(2))
        if failed > 0:
            results['_vitest_tests_failed_'] = 'FAILED'
        if passed > 0:
            results['_vitest_tests_passed_'] = 'PASSED'

    
    if file_test_map:
        results['__file_test_map__'] = json.dumps(file_test_map)

    return results


@_strip_ansi_input
def parse_ginkgo_output(output: str) -> Dict[str, str]:
    """Parse ginkgo test output."""
    results = {}

    
    output = strip_ansi(output)

    for line in output.split('\n'):
        line_stripped = line.strip()

        # • [FAILED] test_name
        if '•' in line_stripped and 'FAILED' in line_stripped:
            match = re.match(r'•\s*\[FAILED\]\s*(.+)', line_stripped)
            if match:
                results[match.group(1).strip()] = 'FAILED'
            continue

        # • test_name (bullet = passed)
        if '•' in line_stripped:
            match = re.match(r'•\s*(.+?)\s*$', line_stripped)
            if match:
                test_name = match.group(1).strip()
                if test_name and 'SUCCESS' not in test_name and 'FAIL' not in test_name and 'Ran' not in test_name:
                    results[test_name] = 'PASSED'
            continue

        # SUCCESS! -- X Passed | Y Failed | Z Pending  (or FAIL! -- ...)
        if 'SUCCESS!' in line_stripped or 'FAIL!' in line_stripped:
            passed_match = re.search(r'(\d+)\s+Passed', line_stripped)
            failed_match = re.search(r'(\d+)\s+Failed', line_stripped)
            if passed_match:
                results['_ginkgo_passed_'] = 'PASSED'
            if failed_match and int(failed_match.group(1)) > 0:
                results['_ginkgo_failed_'] = 'FAILED'

        # Ran N of N Specs in Xs
        ran_match = re.match(r'Ran\s+(\d+)\s+of\s+(\d+)\s+Specs?\s+in\s+[\d.]+s', line_stripped)
        if ran_match:
            results['_ginkgo_specs_run_'] = 'PASSED'

        # --- PASS/FAIL: also parse go test-style suboutput within ginkgo
        match = re.match(r'^--- (PASS|FAIL|SKIP):\s+(\S+)(?:\s|$)', line_stripped)
        if match:
            test_name = match.group(2)
            if '/' not in test_name:
                results[test_name] = match.group(1) + 'ED'

    # Ginkgo ran X suite in Ys  /  Test Suite Passed / Failed
    if 'Test Suite Failed' in output:
        results['_ginkgo_suite_failed_'] = 'FAILED'
    elif 'Test Suite Passed' in output:
        results['_ginkgo_suite_passed_'] = 'PASSED'

    # "Ginkgo ran N suite" summary
    ginkgo_ran = re.search(r'Ginkgo ran\s+(\d+)\s+suite', output)
    if ginkgo_ran:
        if not any(k.startswith('_ginkgo_') for k in results):
            results['_ginkgo_ran_suites_'] = 'PASSED'

    return results


@_strip_ansi_input
def parse_cpp_test_output(output: str) -> Dict[str, str]:
    """Parse cpp test test output."""
    results = {}

    
    output = strip_ansi(output)

    
    # [ RUN      ] TestSuite.TestName
    # [       OK ] TestSuite.TestName (0 ms)
    # [  FAILED  ] TestSuite.TestName (0 ms)
    current_test = None
    for line in output.split('\n'):
        line_stripped = line.strip()

        # [ RUN      ] TestSuite.TestName
        # Use re.search instead of re.match to handle ctest prefix (e.g. "15: [ RUN ]")
        run_match = re.search(r'\[\s*RUN\s*\]\s+(\S+)', line_stripped)
        if run_match:
            current_test = run_match.group(1)
            continue

        # [       OK ] TestSuite.TestName
        ok_match = re.search(r'\[\s*OK\s*\]\s+(\S+)', line_stripped)
        if ok_match:
            results[ok_match.group(1)] = 'PASSED'
            current_test = None
            continue

        # [  FAILED  ] TestSuite.TestName
        fail_match = re.search(r'\[\s*FAILED\s*\]\s+(\S+)', line_stripped)
        if fail_match:
            results[fail_match.group(1)] = 'FAILED'
            current_test = None
            continue

        # FAILED TestSuite.TestName
        if current_test and 'FAILED' in line_stripped:
            results[current_test] = 'FAILED'
            current_test = None

    
    # [  PASSED  ] X tests.
    # [  FAILED  ] Y tests, listed below:
    passed_match = re.search(r'\[\s*PASSED\s*\]\s*(\d+)\s+tests?', output, re.IGNORECASE)
    failed_match = re.search(r'\[\s*FAILED\s*\]\s*(\d+)\s+tests?', output, re.IGNORECASE)

    if passed_match:
        results['_cpp_passed_'] = 'PASSED'
    if failed_match and int(failed_match.group(1)) > 0:
        results['_cpp_failed_'] = 'FAILED'

    # X FAILED TEST
    failed_test_match = re.search(r'(\d+)\s+FAILED\s+TEST', output, re.IGNORECASE)
    if failed_test_match and int(failed_test_match.group(1)) > 0:
        results['_cpp_failed_tests_'] = 'FAILED'

    return results


def parse_ctest_output(output: str) -> Dict[str, str]:
    """Parse ctest test output."""
    results = {}
    clean_output = strip_ansi(output)

    # Pass 1: Parse ctest-level results (binary-level)
    # Pattern: 1/N Test #M: test_name ... Passed/Failed
    # Also handles: N/M Test #M: test_name ... Passed/Failed (older format)
    for line in clean_output.split('\n'):
        stripped = line.strip()

        # 1/N Test #M: test_name ... Passed
        m = re.match(
            r'^\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.{3,}\s*(Passed|Failed)\b',
            stripped, re.IGNORECASE
        )
        if m:
            test_name = m.group(1).strip()
            status = 'PASSED' if m.group(2).lower() == 'passed' else 'FAILED'
            results[test_name] = status
            continue

        # Older ctest: Start N: test_name ... Passed/Failed
        m = re.match(
            r'^Start\s+\d+:\s+(.+?)\s+\.{3,}\s*(Passed|Failed)\b',
            stripped, re.IGNORECASE
        )
        if m:
            test_name = m.group(1).strip()
            status = 'PASSED' if m.group(2).lower() == 'passed' else 'FAILED'
            results[test_name] = status
            continue

        # Redis/Valkey TCL runtest format: [ok]: test name [(N ms)] / [err]: test name in script.tcl
        m = re.match(r'^\[ok\]:\s+(.+?)(?:\s*\(\d+\s*ms\))?\s*$', stripped)
        if m:
            results[m.group(1).strip()] = 'PASSED'
            continue
        m = re.match(r'^\[err\]:\s+(.+?)(?:\s+in\s+\S+)?\s*$', stripped)
        if m:
            results[m.group(1).strip()] = 'FAILED'
            continue

        # Valkey/C unit test format: [ok] - file:test_name / [fail] - file:test_name
        m = re.match(r'^\[(ok|pass)\]\s*-\s*(.+)$', stripped)
        if m:
            results[m.group(2).strip()] = 'PASSED'
            continue
        m = re.match(r'^\[(fail|error)\]\s*-\s*(.+)$', stripped)
        if m:
            results[m.group(2).strip()] = 'FAILED'
            continue

        # TAP + Check format: ok N - name: Passed / not ok N - name: Failed
        m = re.match(r'^ok\s+\d+\s+-\s+(.+?):\s*(Passed|Failed)\s*$', stripped, re.IGNORECASE)
        if m:
            results[m.group(1).strip()] = 'PASSED' if m.group(2).lower() == 'passed' else 'FAILED'
            continue
        m = re.match(r'^not\s+ok\s+\d+\s+-\s+(.+?)(?:\s*:?\s*Failed)?\s*$', stripped, re.IGNORECASE)
        if m:
            results[m.group(1).strip()] = 'FAILED'
            continue

        # Plain TAP: ok N - name (without :Passed suffix)
        m = re.match(r'^ok\s+\d+\s+-\s+(.+)$', stripped)
        if m:
            name = m.group(1).strip()
            if name not in results:
                results[name] = 'PASSED'
            continue

    # If we found ctest-level results AND also have sub-test output,
    # prioritize sub-test results (more granular)
    # Check for Google Test and Check framework patterns within ctest output
    cpp_results = parse_cpp_test_output(output)
    check_results = parse_check_output(output)

    # Filter out Check parser false positives that match ctest result lines
    # e.g. "3/5 Test #3: regression-test ...   Failed" should NOT be a Check test name
    _ctest_line_re = re.compile(r'^\d+/\d+\s+Test\s+#\d+:')
    _ctest_start_re = re.compile(r'^Start\s+\d+:')
    for k in list(check_results.keys()):
        if _ctest_line_re.match(k) or _ctest_start_re.match(k) or k.startswith('_check_pct_'):
            del check_results[k]

    # If sub-test parsers found individual results, merge them in (they're more granular)
    # Sub-test names like "TestSuite.TestName" or "file.c:123: Check ..." are more specific
    # than ctest binary names
    has_sub_tests = any(
        not k.startswith('_') for k in list(cpp_results.keys()) + list(check_results.keys())
    )

    if has_sub_tests:
        # Merge sub-test results, overriding ctest binary-level entries
        for k, v in check_results.items():
            if not k.startswith('_'):
                results[k] = v
        for k, v in cpp_results.items():
            # Skip false positives from Google Test summary like "1" from "[  FAILED  ] 1 tests."
            if not k.startswith('_') and not re.match(r'^\d+$', k):
                results[k] = v
        # Merge summary markers from sub-test parsers
        for k, v in check_results.items():
            if k.startswith('_'):
                results[k] = v
        for k, v in cpp_results.items():
            if k.startswith('_'):
                results[k] = v
    else:
        # No sub-tests found — use ctest binary-level results only
        # Also add summary markers if available
        if cpp_results:
            for k, v in cpp_results.items():
                if k.startswith('_'):
                    results[k] = v
        if check_results:
            for k, v in check_results.items():
                if k.startswith('_'):
                    results[k] = v

    # Summary: X% tests passed, Y% tests failed
    summary_m = re.search(
        r'(\d+)%\s+tests\s+passed,\s*(\d+)%\s+tests\s+failed',
        clean_output, re.IGNORECASE
    )
    if summary_m:
        pass_pct = int(summary_m.group(1))
        fail_pct = int(summary_m.group(2))
        if fail_pct > 0 and not any(v == 'FAILED' for v in results.values() if not k.startswith('_')):
            results['_ctest_failed_'] = 'FAILED'
        if pass_pct > 0 and not any(v == 'PASSED' for v in results.values() if not k.startswith('_')):
            results['_ctest_passed_'] = 'PASSED'

    return results


@_strip_ansi_input
def parse_check_output(output: str) -> Dict[str, str]:
    """Parse check test output."""
    results = {}
    output = strip_ansi(output)

    # Pass 1: Detect "Test function_name:" structure and group assertions under tests.
    # When Test headers exist, use them as status_map keys, AND capture assertion-level
    # names (file.c:NN: Check description) for F2P/P2P matching at check granularity.
    test_results = {}  # test_name -> 'PASSED' or 'FAILED'
    assertion_results = {}  # "file.c:NN: Check description" -> 'PASSED' or 'FAILED'
    has_test_headers = False
    current_test = None
    _test_header_re = re.compile(r'^Test\s+(.+?)\s*:\s*$')
    _check_assertion_re = re.compile(r'^(\S+):(\d+):\s+Check\s+(.+?)\.\.\.\s*(ok|failed|FAILED|OK)\s*$', re.IGNORECASE)

    for line in output.split('\n'):
        stripped = line.strip()
        m = _test_header_re.match(stripped)
        if m:
            has_test_headers = True
            current_test = m.group(1).strip()
            if current_test not in test_results:
                test_results[current_test] = 'PASSED'
            continue
        # Assertion line: "file.c:NNN: Check xxx... ok/failed"
        # Capture both test-level status and assertion-level name
        assert_m = _check_assertion_re.match(stripped)
        if assert_m:
            status = 'PASSED' if assert_m.group(4).lower() == 'ok' else 'FAILED'
            assertion_name = f"{assert_m.group(1)}:{assert_m.group(2)}: Check {assert_m.group(3)}"
            assertion_results[assertion_name] = status
            if current_test:
                if status == 'FAILED':
                    test_results[current_test] = 'FAILED'
            # Also capture file:line format (shorter, for compatibility)
            short_name = f"{assert_m.group(1)}:{assert_m.group(2)}"
            if short_name not in assertion_results:
                assertion_results[short_name] = status
        elif current_test:
            if re.search(r'\.\.\.\s*failed\s*$', stripped, re.IGNORECASE):
                test_results[current_test] = 'FAILED'

    # Always merge assertion-level results (they are more granular than test-level)
    results.update(assertion_results)
    if has_test_headers:
        # Also add test-level results (but assertion-level takes priority since already merged)
        for k, v in test_results.items():
            if k not in results:
                results[k] = v

    # Pass 2: If no Test headers found AND no assertion results, fall back to per-assertion parsing
    if not has_test_headers and not assertion_results:
        for line in output.split('\n'):
            stripped = line.strip()
            # "  test_name ... ok" / "  file.c:NN Check xxx... ok"
            m = re.match(r'^(.+?)\.{3,}\s*ok\s*$', stripped, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                if name and len(name) > 1:
                    results[name] = 'PASSED'
                continue
            # "  test_name ... FAILED"
            m = re.match(r'^(.+?)\.{3,}\s*FAILED\s*$', stripped, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                if name and len(name) > 1:
                    results[name] = 'FAILED'
                continue
            # "  test_name:ok" / "  test_name:FAILED" (colon variant)
            m = re.match(r'^(.+?):\s*ok\s*$', stripped, re.IGNORECASE)
            if m:
                results[m.group(1).strip()] = 'PASSED'
                continue
            m = re.match(r'^(.+?):\s*FAILED\s*$', stripped, re.IGNORECASE)
            if m:
                results[m.group(1).strip()] = 'FAILED'
                continue

    # Summary: "X unit tests: Y passed, Z failed" or "X unit tests, Y passed, Z failed"
    summary_m = re.search(
        r'(\d+)\s+unit\s+tests?[:\s]+(\d+)\s+passed\D+(\d+)\s+failed',
        output, re.IGNORECASE
    )
    if summary_m:
        total = int(summary_m.group(1))
        passed = int(summary_m.group(2))
        failed = int(summary_m.group(3))
        if failed > 0:
            results['_check_summary_failed_'] = str(failed)
        if passed > 0:
            results['_check_summary_passed_'] = str(passed)

    # "Count of all unit tests: X  Count of run unit tests: Y  Count of failed unit tests: Z"
    count_m = re.search(
        r'Count of (?:all |)unit tests:\s*(\d+).*?Count of (?:run |)unit tests:\s*(\d+).*?Count of failed unit tests:\s*(\d+)',
        output, re.IGNORECASE
    )
    if count_m and not summary_m:
        total = int(count_m.group(2))
        failed = int(count_m.group(3))
        passed = total - failed
        if failed > 0:
            results['_check_summary_failed_'] = str(failed)
        if passed > 0:
            results['_check_summary_passed_'] = str(passed)

    # "X% tests passed, Y% tests failed" or "X% tests failed"
    pct_m = re.search(r'(\d+)%\s+tests?\s+passed', output, re.IGNORECASE)
    if pct_m and not summary_m and not count_m:
        # Only percentage, no absolute counts — add summary flag
        results['_check_pct_passed_'] = pct_m.group(1)

    # "Running suite(s): N"
    suite_m = re.search(r'Running suite\S*:\s*(\d+)', output, re.IGNORECASE)

    # SUCCESS: All conditions have passed / All unit tests have passed
    if re.search(r'SUCCESS:\s*All\s+(?:conditions|unit tests)\s+have\s+passed', output, re.IGNORECASE):
        if not any(v == 'FAILED' for v in results.values()):
            results['_check_all_passed_'] = 'PASSED'

    # FAIL: All conditions have passed (contradiction) — check for explicit failures
    if re.search(r'FAILED:\s*All\s+conditions\s+have\s+passed', output, re.IGNORECASE):
        # Check format inconsistency — look for actual failure markers
        pass

    return results


@_strip_ansi_input
def parse_xunit_output(output: str) -> Dict[str, str]:
    """Parse xunit test output."""
    results = {}

    
    output = strip_ansi(output)

    # [xUnit.net 00:00:01.96]     Test.Name [FAIL]
    for line in output.split('\n'):
        line_stripped = line.strip()

        
        xunit_match = re.match(r'\[xUnit\.net\s+[\d:.]+\]\s+(.+?)\s+\[(FAIL|SKIP|PASS)\]', line_stripped)
        if xunit_match:
            test_name = xunit_match.group(1).strip()
            status = xunit_match.group(2)
            if status == 'FAIL':
                results[test_name] = 'FAILED'
            elif status == 'SKIP':
                results[test_name] = 'SKIPPED'
            else:
                results[test_name] = 'PASSED'
            continue

        # Failed Test.Name [X ms]
        fail_match = re.match(r'Failed\s+(.+?)\s+\[', line_stripped)
        if fail_match:
            results[fail_match.group(1).strip()] = 'FAILED'
            continue

        # Skipped Test.Name [X ms]
        skip_match = re.match(r'Skipped\s+(.+?)\s+\[', line_stripped)
        if skip_match:
            results[skip_match.group(1).strip()] = 'SKIPPED'
            continue

    
    summary_match = re.search(r'Failed!\s+-\s+Failed:\s*(\d+).*Passed:\s*(\d+).*Total:\s*(\d+)', output, re.IGNORECASE)
    if summary_match:
        failed = int(summary_match.group(1))
        passed = int(summary_match.group(2))
        if failed > 0:
            results['_xunit_failed_'] = 'FAILED'
        if passed > 0:
            results['_xunit_passed_'] = 'PASSED'

    return results


@_strip_ansi_input
def parse_tslint_output(output: str) -> Dict[str, str]:
    """Parse tslint test output."""
    results = {}

    
    output = strip_ansi(output)

    
    for line in output.split('\n'):
        line_stripped = line.strip()

        # test/rules/.../test.ts.lint: Passed
        match = re.match(r'(test/rules/\S+):\s*(Passed|Failed|Skipped)', line_stripped)
        if match:
            test_name = match.group(1)
            status = match.group(2)
            if status == 'Passed':
                results[test_name] = 'PASSED'
            elif status == 'Failed':
                results[test_name] = 'FAILED'
            elif status == 'Skipped':
                results[test_name] = 'SKIPPED'
            continue

        
        match = re.match(r'(test/\S+):\s*(Passed|Failed|Skipped)', line_stripped)
        if match:
            test_name = match.group(1)
            status = match.group(2)
            if status == 'Passed':
                results[test_name] = 'PASSED'
            elif status == 'Failed':
                results[test_name] = 'FAILED'
            elif status == 'Skipped':
                results[test_name] = 'SKIPPED'

    return results


@_strip_ansi_input
def parse_web_test_runner_output(output: str) -> Dict[str, str]:
    """Parse web test runner test output."""
    results = {}

    
    output = strip_ansi(output)

    
    
    for line in output.split('\n'):
        line_stripped = line.strip()

        # Chromium: |██████████████████████████████| 1/1 test files | 8 passed, 3 failed
        match = re.search(r'(?:Chromium|Firefox|Webkit|Safari|Chrome):\s*\|.*\|\s*(\d+)\s+passed,\s*(\d+)\s+failed', line_stripped, re.IGNORECASE)
        if match:
            passed = int(match.group(1))
            failed = int(match.group(2))
            if failed > 0:
                results['_web_test_runner_failed_'] = 'FAILED'
            if passed > 0:
                results['_web_test_runner_passed_'] = 'PASSED'
            continue

        # Finished running tests in 38.9s with 2 failed
        match = re.search(r'Finished running tests.*with\s+(\d+)\s+failed', line_stripped, re.IGNORECASE)
        if match:
            results['_web_test_runner_failed_'] = 'FAILED'

    return results


def parse_go_testify_output(output: str) -> Dict[str, str]:
    """Parse go testify test output."""
    results = {}

    
    for line in output.split('\n'):
        line_stripped = line.strip()

        # --- FAIL: TestName (0.00s)
        match = re.match(r'---\s*FAIL:\s*(\S+)', line_stripped)
        if match:
            results[match.group(1)] = 'FAILED'
            continue

        # --- PASS: TestName (0.00s)
        match = re.match(r'---\s*PASS:\s*(\S+)', line_stripped)
        if match:
            results[match.group(1)] = 'PASSED'
            continue

    
    tests_match = re.search(r'Tests:\s*(\d+)', output)
    failures_match = re.search(r'Failures?:\s*(\d+)', output, re.IGNORECASE)

    if tests_match and failures_match:
        total = int(tests_match.group(1))
        failed = int(failures_match.group(1))
        passed = total - failed
        if failed > 0:
            results['_testify_failed_'] = 'FAILED'
        if passed > 0:
            results['_testify_passed_'] = 'PASSED'

    return results


def parse_maven_individual_output(output: str) -> Dict[str, str]:
    """Parse maven individual test output."""
    results = {}
    pending_tests = []
    unmatched_results = []

    test_name_pattern = r'^.*-Dtest=(\S+).*$'
    result_pattern = r'^.*BUILD\s+(SUCCESS|FAILURE)\s*$'

    for line in output.split('\n'):
        stripped = line.strip()
        test_name_match = re.match(test_name_pattern, stripped)
        if test_name_match:
            # Strip trailing non-test chars (e.g. quotes, semicolons)
            name = test_name_match.group(1).rstrip("'\";")
            pending_tests.append(name)
            continue

        result_match = re.match(result_pattern, stripped)
        if result_match:
            status = result_match.group(1)
            if pending_tests:
                test_name = pending_tests.pop(0)
                results[test_name] = 'PASSED' if status == 'SUCCESS' else 'FAILED'
            else:
                unmatched_results.append(status)
            continue

    # Match remaining pending tests with unmatched results
    while pending_tests and unmatched_results:
        test_name = pending_tests.pop(0)
        status = unmatched_results.pop(0)
        results[test_name] = 'PASSED' if status == 'SUCCESS' else 'FAILED'

    if pending_tests:
        logger.debug(f"Maven individual parser: {len(pending_tests)} test(s) had no BUILD result: {pending_tests}")

    return results


def parse_ant_junit_output(output: str) -> Dict[str, str]:
    """Parse ant junit test output."""
    results = {}
    pattern = r'^\s*\[junit\]\s+\[(PASS|FAIL|ERR)\]\s+(.+)$'

    for line in output.split('\n'):
        match = re.match(pattern, line.strip())
        if match:
            status, test_name = match.groups()
            test_name = test_name.strip()
            if status == 'PASS':
                results[test_name] = 'PASSED'
            elif status in ('FAIL', 'ERR'):
                results[test_name] = 'FAILED'

    return results


def parse_maven_output(output: str) -> Dict[str, str]:
    """Parse maven test output."""
    results = {}
    clean_output = strip_ansi(output)
    current_class = None  # Track last "Running ClassName" for context
    in_failures_section = False
    in_errors_section = False
    # Track classes that failed (for class-level fallback when no individual methods)
    class_failures = {}  # class_name -> 'FAILED' or 'ERROR'
    # Track which classes have individual method entries
    classes_with_methods = set()
    # Track classes where all tests passed (for class-level PASSED entries)
    class_passes = set()

    # ---- SWE-bench style: match -Dtest= lines with BUILD SUCCESS/FAILURE ----
    # When eval_script uses set -x, each -Dtest= command is traced, and the
    # corresponding BUILD SUCCESS/FAILURE follows. This gives us method-level results.
    dtest_pattern = re.compile(r'^.*-Dtest=(\S+).*$')
    build_result_pattern = re.compile(r'^.*BUILD\s+(SUCCESS|FAILURE).*$')
    pending_dtests = []
    unmatched_build_results = []
    for line in clean_output.split('\n'):
        m = dtest_pattern.match(line.strip())
        if m:
            pending_dtests.append(m.group(1))
            continue
        m = build_result_pattern.match(line.strip())
        if m:
            status = m.group(1)
            if pending_dtests:
                test_name = pending_dtests.pop(0)
                results[test_name] = 'PASSED' if status == 'SUCCESS' else 'FAILED'
                # Also add class-level entry for F2P/P2P matching
                if '#' in test_name:
                    cls_name = test_name.split('#')[0]
                    if cls_name not in results:
                        results[cls_name] = 'PASSED' if status == 'SUCCESS' else 'FAILED'
                elif '.' in test_name:
                    # Already a class name
                    pass
            else:
                unmatched_build_results.append(status)

    # Match remaining pending tests with unmatched build results (FIFO)
    while pending_dtests and unmatched_build_results:
        test_name = pending_dtests.pop(0)
        status = unmatched_build_results.pop(0)
        results[test_name] = 'PASSED' if status == 'SUCCESS' else 'FAILED'
        if '#' in test_name:
            cls_name = test_name.split('#')[0]
            if cls_name not in results:
                results[cls_name] = 'PASSED' if status == 'SUCCESS' else 'FAILED'

    # If we got results from -Dtest= matching, use those as authoritative and
    # skip the Surefire parsing below (which would overwrite with class-level entries)
    if results:
        # Check for BUILD FAILURE fallback (no -Dtest commands at all)
        if not any(v in ('PASSED', 'FAILED') for v in results.values()):
            if re.search(r'\[(?:INFO|ERROR)\]\s*(?:-+\s*)?BUILD FAILURE', clean_output, re.IGNORECASE):
                results['_maven_build_failure_'] = 'FAILED'
        return results

    # ---- Surefire output parsing (fallback when no -Dtest= lines found) ----
    for line in clean_output.split('\n'):
        stripped = line.strip()

        # Track current class from "Running" lines
        run_match = re.match(r'\[(?:INFO|ERROR)\]\s+Running\s+(\S+)', stripped)
        if run_match:
            current_class = run_match.group(1)
            in_failures_section = False
            in_errors_section = False
            continue

        # Per-test FAILED: [ERROR] methodName(ClassName)  Time elapsed: X s  <<< FAILURE!
        # Also handles: methodName[param1](ClassName)  Time elapsed: X s  <<< FAILURE!
        fail_match = re.match(
            r'\[ERROR\]\s+(\S+)\(([^)]+)\)\s+Time elapsed:\s*[\d.]+\s*s?\s*<<<\s*FAILURE!',
            stripped
        )
        if fail_match:
            method_name = fail_match.group(1)
            class_name = fail_match.group(2)
            full_name = f"{class_name}.{method_name}"
            results[full_name] = 'FAILED'
            classes_with_methods.add(class_name)
            in_failures_section = False
            in_errors_section = False
            continue

        # Per-test ERROR: [ERROR] methodName(ClassName)  Time elapsed: X s  <<< ERROR!
        error_match = re.match(
            r'\[ERROR\]\s+(\S+)\(([^)]+)\)\s+Time elapsed:\s*[\d.]+\s*s?\s*<<<\s*ERROR!',
            stripped
        )
        if error_match:
            method_name = error_match.group(1)
            class_name = error_match.group(2)
            full_name = f"{class_name}.{method_name}"
            results[full_name] = 'ERROR'
            classes_with_methods.add(class_name)
            in_failures_section = False
            in_errors_section = False
            continue

        # Surefire 3.x format: [ERROR] full.class.Name.methodName -- Time elapsed: X s <<< FAILURE!
        # [INFO] full.class.Name.methodName -- Time elapsed: X s
        surefire3_match = re.match(
            r'\[(?:ERROR|INFO)\]\s+([\w.$]+)\.(\w+)\s+--\s+Time elapsed:\s*[\d.]+\s*s?',
            stripped
        )
        if surefire3_match:
            class_name = surefire3_match.group(1)
            method_name = surefire3_match.group(2)
            full_name = f"{class_name}.{method_name}"
            is_failure = '<<< FAILURE!' in stripped or '<<< ERROR!' in stripped
            is_error = '<<< ERROR!' in stripped
            if is_error:
                results[full_name] = 'ERROR'
            elif is_failure:
                results[full_name] = 'FAILED'
            elif full_name not in results:
                results[full_name] = 'PASSED'
            classes_with_methods.add(class_name)
            in_failures_section = False
            in_errors_section = False
            continue

        # Per-test PASSED (with -Dsurefire.useFile=false):
        # [INFO]   methodName  Time elapsed: X s
        # Must have "Time elapsed" to distinguish from other [INFO] lines
        pass_match = re.match(
            r'\[INFO\]\s+(\S+)\s+Time elapsed:\s*[\d.]+\s*s?\s*$',
            stripped
        )
        if pass_match:
            method_name = pass_match.group(1)
            # Strip (ClassName) suffix from INFO lines like "testMethod(ClassName) Time elapsed"
            paren_match = re.match(r'^(.+?)\((\S+)\)$', method_name)
            if paren_match:
                method_name = paren_match.group(1)
                if not current_class:
                    current_class = paren_match.group(2)
            if current_class:
                full_name = f"{current_class}.{method_name}"
                classes_with_methods.add(current_class)
            else:
                full_name = method_name
            if full_name not in results:
                results[full_name] = 'PASSED'
            in_failures_section = False
            in_errors_section = False
            continue

        # Failure list section: [ERROR] Failures:
        if re.match(r'\[ERROR\]\s+Failures:\s*$', stripped):
            in_failures_section = True
            in_errors_section = False
            continue
        # Error list section: [ERROR] Errors:
        if re.match(r'\[ERROR\]\s+Errors:\s*$', stripped):
            in_errors_section = True
            in_failures_section = False
            continue

        # In Failures/Errors section: [ERROR]   methodName(ClassName)
        if in_failures_section or in_errors_section:
            fe_match = re.match(r'\[ERROR\]\s+(\S+)\(([^)]+)\)', stripped)
            if fe_match:
                method_name = fe_match.group(1)
                class_name = fe_match.group(2)
                full_name = f"{class_name}.{method_name}"
                results[full_name] = 'FAILED' if in_failures_section else 'ERROR'
                classes_with_methods.add(class_name)
                continue
            # End of section if line doesn't match
            if stripped and not re.match(r'\[ERROR\]\s+\S', stripped):
                in_failures_section = False
                in_errors_section = False

        # Tests run summary per class:
        # [INFO|ERROR] Tests run: N, Failures: M, Errors: E, ..., Time elapsed: Xs <<< FAILURE! -- in ClassName
        # Note: Maven uses both "- in" and "-- in" (double dash is common)
        tr_class_match = re.match(
            r'\[(?:INFO|ERROR)\]\s+Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)(?:,\s*Skipped:\s*(\d+))?.*?(?:<<<\s*FAILURE!\s*)?(?:-{1,2})\s+in\s+(\S+)',
            stripped
        )
        if tr_class_match:
            failures = int(tr_class_match.group(2))
            errors = int(tr_class_match.group(3))
            class_name = tr_class_match.group(5)
            if failures > 0 or errors > 0:
                class_failures[class_name] = 'FAILED' if failures > 0 else 'ERROR'
            else:
                class_passes.add(class_name)
            continue

        # Tests run summary without class name
        tr_match = re.match(
            r'\[(?:INFO|ERROR)\]\s+Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)(?:,\s*Skipped:\s*(\d+))?',
            stripped
        )
        if tr_match:
            failures = int(tr_match.group(2))
            errors = int(tr_match.group(3))
            if failures > 0 or errors > 0:
                if current_class:
                    class_failures[current_class] = 'FAILED' if failures > 0 else 'ERROR'
                else:
                    class_failures['_unknown_'] = 'FAILED'
            else:
                if current_class:
                    class_passes.add(current_class)
            continue

        # Legacy: [INFO] ClassName PASSED/FAILED (rare in standard Maven output)
        match = re.match(r'\[(?:INFO|ERROR|WARN)\]\s+(\S+)\s+(PASSED|FAILED)', stripped)
        if match:
            results[match.group(1)] = match.group(2)
            continue

        # Surefire report TXT format: testName(com.example.MyTest) PASSED/FAILED
        # or: com.example.MyTest.testMethod PASSED/FAILED
        # These come from cat of target/surefire-reports/TEST-*.txt
        sr_match = re.match(r'^(\S+(?:\([^)]*\))?)\s+(PASSED|FAILED)\s*$', stripped)
        if sr_match:
            name = sr_match.group(1)
            status = sr_match.group(2)
            # Convert methodName(ClassName) to ClassName.methodName
            m = re.match(r'^(\w+)\(([^)]+)\)$', name)
            if m:
                full_name = f"{m.group(2)}.{m.group(1)}"
            else:
                full_name = name
            if full_name not in results:
                results[full_name] = status
            continue

    # Surefire XML report format: <testcase name="methodName" classname="com.example.MyTest" .../>
    # These come from cat of target/surefire-reports/TEST-*.xml (appended by _append_test_reports)
    for xml_match in re.finditer(
        r'<testcase\s+[^>]*name=["\']([^"\']+)["\']\s+[^>]*classname=["\']([^"\']+)["\'][^>]*(?:/>|>(.*?)</testcase>)',
        clean_output, re.DOTALL
    ):
        method_name = xml_match.group(1)
        class_name = xml_match.group(2)
        body = xml_match.group(3) or ''
        full_name = f"{class_name}.{method_name}"
        if '<failure' in body or '<error' in body:
            results[full_name] = 'FAILED'
        elif full_name not in results:
            results[full_name] = 'PASSED'

    # Add class-level failures for classes that have NO individual method entries
    # Use the class name directly (not a placeholder) so F2P matching works
    for cls_name, status in class_failures.items():
        if cls_name == '_unknown_':
            results['_maven_class_failed_'] = status
        elif cls_name not in classes_with_methods:
            results[cls_name] = status
        elif cls_name not in results:
            # Class has some method-level FAILED entries — add class-level alias
            # so F2P with class name can match even when method-level names exist
            results[cls_name] = status

    # Add class-level PASSED entries for classes with 0 failures and no individual method entries
    # This is crucial for F2P matching: when a test class had failures before the patch
    # but passes after, the class name needs to appear as PASSED in the after log
    for cls_name in class_passes:
        if cls_name not in classes_with_methods and cls_name not in results:
            results[cls_name] = 'PASSED'
        elif cls_name in classes_with_methods and cls_name not in results:
            # Class has method-level entries and all passed — add class-level alias
            # so F2P with class name (e.g. com.example.MyTest) can match when
            # method-level entries (com.example.MyTest.testMethod) are present
            all_methods_pass = all(
                results.get(f"{cls_name}.{m}") == 'PASSED'
                for m in [k.split('.')[-1] for k in results if k.startswith(f"{cls_name}.") and k != cls_name]
            )
            if all_methods_pass:
                results[cls_name] = 'PASSED'

    # Maven BUILD FAILURE detection (fallback when no test results at all)
    if re.search(r'\[(?:INFO|ERROR)\]\s*(?:-+\s*)?BUILD FAILURE', clean_output, re.IGNORECASE):
        has_individual = any(v in ('PASSED', 'FAILED') and not k.startswith('_') for k, v in results.items())
        if not has_individual and not class_failures:
            results['_maven_build_failure_'] = 'FAILED'

    return results


def parse_gradle_output(output: str) -> Dict[str, str]:
    """Parse gradle test output."""
    results = {}
    for line in output.split('\n'):
        stripped = strip_ansi(line).strip()

        # Per-test: ClassName > methodName PASSED/FAILED
        match = re.match(r'^(\S+(?:\s+\S+)*?)\s*>\s*(\S+(?:\(\))?)\s+(PASSED|FAILED)\s*$', stripped)
        if match:
            test_name = f"{match.group(1)} > {match.group(2)}"
            results[test_name] = match.group(3)
            continue

        # Error listing: - org.apache.lucene.facet.TestDrillSideways.testDrillSidewaysSearchUseCorrectIterator (:lucene:facet)
        err_match = re.match(r'^-\s+((?:\w+\.)*\w+)\.(\w+)\s*\(:?\s*\S+\s*\)\s*$', stripped)
        if err_match and 'test' in err_match.group(2).lower():
            test_name = f"{err_match.group(1)} > {err_match.group(2)}"
            results.setdefault(test_name, 'FAILED')
            continue

    # Summary: Gradle Test Run :module:test Results: FAILED (128 tests, 125 successes, 3 failures, 0 skipped)
    summary_match = re.search(
        r'Gradle Test Run\s+\S+\s+Results:\s+(SUCCESS|FAILED)\s+\((\d+)\s+tests?,\s+(\d+)\s+successes?,\s+(\d+)\s+failures?',
        output
    )
    if summary_match:
        status = summary_match.group(1)
        total = int(summary_match.group(2))
        passed = int(summary_match.group(3))
        failed = int(summary_match.group(4))
        results['_gradle_total_'] = str(total)
        results['_gradle_passed_'] = str(passed)
        results['_gradle_failed_'] = str(failed)
        if failed > 0:
            results['_gradle_summary_failed_'] = 'FAILED'
        if status == 'FAILED':
            results['_gradle_build_failed_'] = 'FAILED'

    # Lucene-style summary: |  Results: FAILURE (11 tests, 10 passed, 1 failed, 0 skipped)  |
    lucene_summary = re.search(
        r'Results:\s+(SUCCESS|FAILURE|FAILED)\s+\((\d+)\s+tests?,\s+(\d+)\s+passed?,\s+(\d+)\s+failed?',
        output
    )
    if lucene_summary and '_gradle_total_' not in results:
        status = lucene_summary.group(1)
        total = int(lucene_summary.group(2))
        passed = int(lucene_summary.group(3))
        failed = int(lucene_summary.group(4))
        results['_gradle_total_'] = str(total)
        results['_gradle_passed_'] = str(passed)
        results['_gradle_failed_'] = str(failed)
        if failed > 0:
            results['_gradle_summary_failed_'] = 'FAILED'
        if status in ('FAILURE', 'FAILED'):
            results['_gradle_build_failed_'] = 'FAILED'

    # Parse JUnit XML from Gradle test-results (appended by _append_test_reports)
    # Format: <testcase name="methodName" classname="com.example.MyTest" .../>
    # or: <testcase name="methodName" classname="com.example.MyTest"><failure/></testcase>
    for xml_match in re.finditer(
        r'<testcase\s+[^>]*name=["\']([^"\']+)["\']\s+[^>]*classname=["\']([^"\']+)["\'][^>]*(?:/>|>(.*?)</testcase>)',
        output, re.DOTALL
    ):
        method_name = xml_match.group(1)
        class_name = xml_match.group(2)
        body = xml_match.group(3) or ''
        full_name = f"{class_name}.{method_name}"
        if '<failure' in body or '<error' in body:
            results[full_name] = 'FAILED'
        elif full_name not in results:
            results[full_name] = 'PASSED'

    # Fallback: BUILD FAILED
    if not results and re.search(r'BUILD FAILED', output):
        results['_gradle_build_failed_'] = 'FAILED'

    return results


def parse_cppinsights_output(output: str) -> Dict[str, str]:
    """Parse cppinsights test output."""
    results = {}
    for line in output.split('\n'):
        stripped = strip_ansi(line).strip()

        # [PASSED]/[FAILED] filename - duration
        match = re.match(r'^\[(PASSED|FAILED)\]\s+(\S+)\s+-', stripped)
        if match:
            results[match.group(2)] = match.group(1)
            continue

        # [PASSED]/[FAILED] Compile: filename
        match = re.match(r'^\[(PASSED|FAILED)\]\s+Compile:\s+(\S+)', stripped)
        if match:
            results[f"Compile:{match.group(2)}"] = match.group(1)
            continue

    # Summary: Tests passed: N/M
    summary = re.search(r'Tests passed:\s+(\d+)/(\d+)', output)
    if summary:
        results['_cppinsights_passed_'] = summary.group(1)
        results['_cppinsights_total_'] = summary.group(2)

    # Insights crashed: N
    crashed = re.search(r'Insights crashed:\s+(\d+)', output)
    if crashed and int(crashed.group(1)) > 0:
        results['_cppinsights_crashed_'] = 'FAILED'

    return results


def parse_redis_output(output: str) -> Dict[str, str]:
    """Parse redis test output."""
    results = {}
    pattern = r'^\[(ok|err|skip|ignore)\]:\s(.+?)(?:\s*\((\d+\s*m?s)\))?$'

    for line in output.split('\n'):
        match = re.match(pattern, line.strip())
        if match:
            status, test_name, _duration = match.groups()
            test_name = test_name.strip()
            if status == 'ok':
                results[test_name] = 'PASSED'
            elif status == 'err':
                # Strip file path info from failed test names
                test_name = re.sub(r'\s+in\s+\S+$', '', test_name)
                results[test_name] = 'FAILED'
            elif status in ('skip', 'ignore'):
                results[test_name] = 'SKIPPED'

    return results


def parse_jq_output(output: str) -> Dict[str, str]:
    """Parse jq test output."""
    results = {}
    pattern = r'^\s*(PASS|FAIL):\s(.+)$'

    for line in output.split('\n'):
        match = re.match(pattern, line.strip())
        if match:
            status, test_name = match.groups()
            test_name = test_name.strip()
            if status == 'PASS':
                results[test_name] = 'PASSED'
            elif status == 'FAIL':
                results[test_name] = 'FAILED'

    return results


def parse_doctest_output(output: str) -> Dict[str, str]:
    """Parse doctest test output."""
    import xml.etree.ElementTree as ET
    results = {}

    start_tag = '<doctest'
    end_tag = '</doctest>'
    start_index = output.find(start_tag)
    end_index = output.find(end_tag, start_index) + len(end_tag) if start_index != -1 else -1

    if start_index != -1 and end_index != -1:
        xml_string = output[start_index:end_index]
        try:
            root = ET.fromstring(xml_string)
            for testcase in root.findall('.//TestCase'):
                testcase_name = testcase.get('name')
                for subcase in testcase.findall('.//SubCase'):
                    subcase_name = subcase.get('name')
                    name = f'{testcase_name} > {subcase_name}'
                    expressions = subcase.findall('.//Expression')
                    subcase_passed = all(
                        expr.get('success') == 'true' for expr in expressions
                    )
                    results[name] = 'PASSED' if subcase_passed else 'FAILED'
        except ET.ParseError:
            pass

    return results


def _strip_phpunit_timing(name: str) -> str:
    """Strip PHPUnit timing suffix like [114.11 ms], [1.234 s], [500 μs] from test names."""
    name = re.sub(r'\s*\[\d+(?:\.\d+)?\s*(?:ms|s|μs|us|secs?)\]\s*$', '', name)
    return name.strip()


def _strip_param_suffix(name: str) -> str:
    """Strip parameterized test suffix like (key=value) or (key1=val1, key2=val2) from test names.

    Handles Django/unittest style: test_foo(nomination_num=6) -> test_foo
    Only applies when the name before the parentheses looks like a Python test method
    (starts with test_ or matches Class.method pattern), to avoid stripping parentheses
    that are part of English test descriptions like:
      "can be controlled (type="default")"
      "Request a page with @login_required(redirect_field_name='redirect_to')"
    """
    # Only strip if the part before ( looks like a Python test identifier:
    # - test_xxx(  (unittest style)
    # - Xxx.yyy(    (Django Class.method style)
    # - module.Class.test_method(  (dotted Python path)
    m = re.match(r'^(.*?(?:test_\w+|(?:[A-Z]\w*\.)*\w+\.\w+))\s*\(\w[\w.]*=\s*[^)]+\)\s*$', name)
    if m:
        return m.group(1).strip()
    return name


def parse_phpunit_output(output: str) -> Dict[str, str]:
    """Parse phpunit test output."""
    results = {}
    for line in output.split('\n'):
        line_stripped = line.strip()

        
        if line_stripped.startswith(('✓', '✔', '√')):
            test_name = line_stripped[1:].strip()
            test_name = _strip_phpunit_timing(test_name)
            if test_name:
                results[test_name] = 'PASSED'
            continue

        
        if line_stripped.startswith(('✘', '✗', '×')):
            test_name = line_stripped[1:].strip()
            test_name = _strip_phpunit_timing(test_name)
            if test_name:
                results[test_name] = 'FAILED'
            continue

        
        
        if line_stripped and line_stripped[0] in ('E', 'F'):
            
            if len(line_stripped) > 1 and (line_stripped[1].isdigit() or line_stripped[1].isspace()):
                test_name = line_stripped[2:].strip() if line_stripped[1].isspace() else line_stripped[1:].strip()
                test_name = _strip_phpunit_timing(test_name)
                if test_name:
                    results[test_name] = 'FAILED'
            continue
    
    
    tests_match = re.search(r'Tests:\s*(\d+)', output)
    failures_match = re.search(r'Failures?:\s*(\d+)', output, re.IGNORECASE)
    errors_match = re.search(r'Errors?:\s*(\d+)', output, re.IGNORECASE)
    
    if failures_match and int(failures_match.group(1)) > 0 and len(results) == 0:
        results['_phpunit_tests_failed_'] = 'FAILED'
    if errors_match and int(errors_match.group(1)) > 0 and len(results) == 0:
        results['_phpunit_tests_error_'] = 'ERROR'
    
    return results


def parse_elixir_output(output: str) -> Dict[str, str]:
    """Parse elixir test output."""
    results = {}
    for line in output.split('\n'):
        line_stripped = line.strip()

        # .test.exs:line: test_name (X ms)
        match = re.match(r'^(.+\.exs):(\d+):\s+(.+?)\s+\(', line_stripped)
        if match:
            results[match.group(3).strip()] = 'PASSED'
            continue

        # 1) test_name (ModuleName)
        match = re.match(r'^(\d+)\)\s+(.+?)\s+\(', line_stripped)
        if match:
            results[match.group(2).strip()] = 'FAILED'
            continue

        # ** (ExUnit.AssertionError)
        if 'AssertionError' in line_stripped:
            
            continue

    
    
    summary_match = re.search(r'(\d+)\s+tests?\s*,\s*(\d+)\s+failures?', output, re.IGNORECASE)
    if summary_match:
        total = int(summary_match.group(1))
        failures = int(summary_match.group(2))
        passed = total - failures
        if failures > 0:
            results['_elixir_failures_'] = str(failures)
        if passed > 0:
            results['_elixir_passed_'] = str(passed)
    elif re.search(r'(\d+)\s+failures?', output, re.IGNORECASE):
        
        failures_match = re.search(r'(\d+)\s+failures?', output, re.IGNORECASE)
        if failures_match and int(failures_match.group(1)) > 0 and len(results) == 0:
            results['_elixir_tests_failed_'] = 'FAILED'

    return results


@_strip_ansi_input
def parse_dart_output(output: str) -> Dict[str, str]:
    """Parse dart test output."""
    results = {}

    
    clean_output = strip_ansi(output)

    
    
    summary_match = re.search(r'\d+:\d+\s+\+(\d+)\s+-(\d+)', clean_output)
    if summary_match:
        passed = int(summary_match.group(1))
        failed = int(summary_match.group(2))
        if failed > 0:
            results['_dart_summary_failed_'] = str(failed)
        if passed > 0:
            results['_dart_summary_passed_'] = str(passed)
        return results

    
    simple_summary = re.search(r'\+(\d+)\s+-(\d+)(?:\s|$)', clean_output)
    if simple_summary:
        passed = int(simple_summary.group(1))
        failed = int(simple_summary.group(2))
        if failed > 0:
            results['_dart_summary_failed_'] = str(failed)
        if passed > 0:
            results['_dart_summary_passed_'] = str(passed)
        return results

    for line in clean_output.split('\n'):
        line_stripped = line.strip()

        
        if line_stripped.startswith(('✓', '✔')):
            test_name = line_stripped[1:].strip()
            if test_name:
                results[test_name] = 'PASSED'
            continue

        
        if line_stripped.startswith(('✗', '✕', '×')):
            test_name = line_stripped[1:].strip()
            if test_name:
                results[test_name] = 'FAILED'
            continue

        
        if re.match(r'^\+\d+\s+tests?\s+passed', line_stripped, re.IGNORECASE):
            match = re.match(r'^\+(\d+)\s+tests?\s+passed', line_stripped, re.IGNORECASE)
            if match:
                results[f'_dart_{match.group(1)}_passed_'] = 'PASSED'
            continue

        
        if re.match(r'^-\d+\s+tests?\s+failed', line_stripped, re.IGNORECASE):
            match = re.match(r'^-(\d+)\s+tests?\s+failed', line_stripped, re.IGNORECASE)
            if match:
                results[f'_dart_{match.group(1)}_failed_'] = 'FAILED'
            continue

    
    all_passed = re.search(r'All\s+(\d+)\s+tests?\s+passed', clean_output, re.IGNORECASE)
    if all_passed and len(results) == 0:
        results['_dart_tests_passed_'] = 'PASSED'

    return results


@_strip_ansi_input
def parse_jerryscript_output(output: str) -> Dict[str, str]:
    """Parse jerryscript test output."""
    results = {}

    
    clean_output = strip_ansi(output)

    
    summary_match = re.search(r'\[summary\].*?(\d+)\s+PASS.*?(\d+)\s+FAIL', clean_output, re.IGNORECASE)
    if summary_match:
        passed = int(summary_match.group(1))
        failed = int(summary_match.group(2))
        if failed > 0:
            results['_jerryscript_summary_failed_'] = str(failed)
        if passed > 0:
            results['_jerryscript_summary_passed_'] = str(passed)
        return results

    
    total_match = re.search(r'TOTAL:\s*(\d+)', clean_output)
    pass_match = re.search(r'PASS:\s*(\d+)', clean_output)
    fail_match = re.search(r'FAIL:\s*(\d+)', clean_output)

    if total_match and pass_match and fail_match:
        passed = int(pass_match.group(1))
        failed = int(fail_match.group(1))
        if failed > 0:
            results['_jerryscript_summary_failed_'] = str(failed)
        if passed > 0:
            results['_jerryscript_summary_passed_'] = str(passed)
        return results

    
    for line in clean_output.split('\n'):
        line = line.strip()
        
        if ': PASS' in line or ': FAIL' in line:
            
            test_name = line.rsplit(':', 1)[0].strip()
            
            if '/' in test_name:
                test_name = test_name.split('/')[-1]
            if ': PASS' in line:
                results[test_name] = 'PASSED'
            else:
                results[test_name] = 'FAILED'

    return results


@_strip_ansi_input
def parse_tap_output(output: str) -> Dict[str, str]:
    """Parse tap test output."""
    results = {}

    
    output = strip_ansi(output)

    
    tests_match = re.search(r'#\s*tests?\s+(\d+)', output, re.IGNORECASE)
    pass_match = re.search(r'#\s*pass\s+(\d+)', output, re.IGNORECASE)
    fail_match = re.search(r'#\s*fail\s+(\d+)', output, re.IGNORECASE)

    if pass_match or fail_match:
        passed = int(pass_match.group(1)) if pass_match else 0
        failed = int(fail_match.group(1)) if fail_match else 0
        if failed > 0:
            results['_tap_summary_failed_'] = str(failed)
        if passed > 0:
            results['_tap_summary_passed_'] = str(passed)
        return results

    
    for line in output.split('\n'):
        line_stripped = line.strip()

        
        if line_stripped.startswith('ok '):
            
            match = re.match(r'^ok\s+(\d+)\s*[-_]?\s*(.*)$', line_stripped)
            if match:
                test_num = match.group(1)
                test_name = match.group(2).strip() if match.group(2) else ''
                
                if test_name:
                    results[f'test_{test_num}_{test_name[:30]}'] = 'PASSED'
                else:
                    results[f'test_{test_num}'] = 'PASSED'
            continue

        # not ok N test_name
        if line_stripped.startswith('not ok '):
            match = re.match(r'^not ok\s+(\d+)\s*[-_]?\s*(.*)$', line_stripped)
            if match:
                test_num = match.group(1)
                test_name = match.group(2).strip() if match.group(2) else ''
                
                if test_name:
                    results[f'test_{test_num}_{test_name[:30]}'] = 'FAILED'
                else:
                    results[f'test_{test_num}'] = 'FAILED'
            continue

    return results


def parse_rspec_output(output: str) -> Dict[str, str]:
    """Parse rspec test output."""
    results = {}
    clean_output = strip_ansi(output)

    # Try documentation format first (more reliable, no truncation issues)
    # RSpec documentation format: indented test names, with (FAILED - N) for failures
    # Lines like:
    #   ClassName
    #     method_name
    #     method_name (FAILED - 1)
    #   Top level test description
    #     nested test (FAILED - 2)
    # Also detected by RSpec summary line ("N examples, M failures") combined with
    # indented "should" lines — even when all tests pass (no FAILED markers).
    has_doc_format = False
    has_rspec_summary = bool(re.search(r'\d+\s+examples?,\s*\d+\s+failures?', clean_output))
    has_should_lines = False  # Track for doc format detection without failures

    for line in clean_output.split('\n'):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # Skip summary lines
        if re.match(r'^\d+\s+examples?', line_stripped, re.IGNORECASE):
            continue
        if re.match(r'^Finished in', line_stripped, re.IGNORECASE):
            continue
        if re.match(r'^Randomized with seed', line_stripped, re.IGNORECASE):
            continue

        # Failure detail section starts with "  1) Failure:" or "  N) Failure:" / "  N) Error:"
        # These are NOT test names - they are detailed error reports
        if re.match(r'^\d+\)\s+(Failure|Error|Pending):', line_stripped):
            continue

        # test_name (FAILED - N) or test_name (FAILED)
        match = re.match(r'^(.+?)\s+\(FAILED\s*[- ]*\s*\d*\)', line_stripped)
        if match:
            name = match.group(1).strip()
            if name and not name.startswith('Failure') and not name.startswith('Error'):
                results[name] = 'FAILED'
                has_doc_format = True
                continue

        # test_name (PENDING: reason)
        match = re.match(r'^(.+?)\s+\(PENDING', line_stripped)
        if match:
            name = match.group(1).strip()
            if name:
                results[name] = 'PASSED'  # pending = not yet implemented, treated as pass
                has_doc_format = True
                continue

        # Detect RSpec documentation format lines (indented "should/it/when/with" descriptions)
        # These indicate doc format even without (FAILED) markers
        if not has_doc_format and has_rspec_summary:
            if re.match(r'^\s+(should|it |when |with |allows |returns |does |has |can |is |are |includes |renders |raises |throws |displays |processes |handles |creates |destroys |updates |deletes |validates |permits |forbids )', line, re.IGNORECASE):
                has_should_lines = True

    # If we have RSpec summary + indented "should" lines but no (FAILED) markers,
    # this is still documentation format — all tests pass.
    if not has_doc_format and has_rspec_summary and has_should_lines:
        has_doc_format = True

    # If we found documentation-format test results, also mark non-failing lines as PASSED
    if has_doc_format:
        # In RSpec documentation format, ALL non-summary lines are test or group entries.
        # Group headers (describe/context) appear in both before and after logs, so they'll
        # become P2P (harmless). The key is capturing all `it` block names as PASSED.
        # Heuristic: lines that are indented (start with spaces) are likely test names or
        # nested groups. Top-level lines are likely describe/context blocks.
        # To be safe, we include all lines as they appear — group headers will naturally
        # match in both before and after logs.
        in_failure_detail = False
        for line in clean_output.split('\n'):
            line_stripped = line.strip()
            if not line_stripped:
                in_failure_detail = False
                continue

            # Skip summary lines
            if re.match(r'^\d+\s+examples?', line_stripped, re.IGNORECASE):
                continue
            if re.match(r'^Finished in', line_stripped, re.IGNORECASE):
                continue
            if re.match(r'^Randomized with seed', line_stripped, re.IGNORECASE):
                continue

            # Failure/Error detail sections start with "N) Failure:" / "N) Error:"
            # Everything until the next blank line is detail, not a test name
            if re.match(r'^\d+\)\s+(Failure|Error|Pending):', line_stripped):
                in_failure_detail = True
                continue
            if in_failure_detail:
                continue

            # Skip lines that are clearly part of failure backtraces
            if re.match(r'^\s+#\s', line) or re.match(r'^\s+/.*:\d+:in\s', line):
                continue

            # test_name (PENDING: reason) already handled above
            # test_name (FAILED - N) already handled above

            # Passing test: any line not already in results and not a known non-test pattern
            if line_stripped not in results:
                # Skip lines that match FAILED/PENDING suffix (already processed above)
                if re.match(r'.+\(FAILED\s*[- ]*\s*\d*\)\s*$', line_stripped):
                    continue
                if re.match(r'.+\(PENDING', line_stripped):
                    continue
                # Skip very short lines (likely not meaningful test names)
                if len(line_stripped) < 2:
                    continue
                # Skip lines that look like file paths or stack traces
                if re.match(r'^/.+:\d+', line_stripped):
                    continue
                # Skip failure detail section headers and lines
                if re.match(r'^(Failures|Errors|Pending examples):?\s*$', line_stripped, re.IGNORECASE):
                    continue
                if re.match(r'^\d+\)\s+(Failure|Error|Pending):', line_stripped):
                    continue
                # Skip failure detail reference lines like "1) TestController DELETE destroy deletes the record"
                if re.match(r'^\d+\)\s+\S', line_stripped):
                    continue
                if re.match(r'^Failure/Error:', line_stripped):
                    continue
                # Skip backtrace lines like "# ./path:line:in `method'"
                if re.match(r'^#\s', line_stripped):
                    continue
                # Skip RSpec expectation/actual lines (only within failure detail sections,
                # since "should ..." lines are valid test names in documentation format)
                if in_failure_detail and re.match(r'^(expected|got|should|should not)\b', line_stripped, re.IGNORECASE):
                    continue
                results[line_stripped] = 'PASSED'

    # Always parse JSON if present — full_description from JSON is canonical
    # and should override short names from documentation format (e.g. "with default"
    # vs "FastlaneCore FastlaneCore::CertChecker shell escaping uses the correct
    # command to import it with default"). Doc format alone produces inconsistent
    # keys that don't match test names stored by extract.
    if True:
        json_start = clean_output.find('{"version":')
        if json_start >= 0:
            json_str = clean_output[json_start:]
            # Find the end of the JSON object
            brace_count = 0
            json_end = 0
            for i, ch in enumerate(json_str):
                if ch == '{':
                    brace_count += 1
                elif ch == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        json_end = i + 1
                        break
            if json_end > 0:
                try:
                    rspec_json = json.loads(json_str[:json_end])
                    for example in rspec_json.get('examples', []):
                        desc = example.get('full_description', '').strip()
                        status = example.get('status', '')
                        if desc and status:
                            if status == 'passed':
                                results[desc] = 'PASSED'
                            elif status == 'failed':
                                results[desc] = 'FAILED'
                            elif status == 'pending':
                                results[desc] = 'PASSED'
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

    # Fallback: look for (FAILED - N) patterns even without doc format
    if len(results) == 0:
        for line in clean_output.split('\n'):
            line_stripped = line.strip()
            match = re.match(r'^(.+?)\s+\(FAILED\s*-\s*\d+\)', line_stripped)
            if match:
                results[match.group(1).strip()] = 'FAILED'
                continue

    # Last resort: summary-based placeholders
    examples_match = re.search(r'(\d+)\s+examples?', clean_output, re.IGNORECASE)
    failures_match = re.search(r'(\d+)\s+failures?', clean_output, re.IGNORECASE)

    if failures_match and int(failures_match.group(1)) > 0 and len(results) == 0:
        results['_rspec_tests_failed_'] = 'FAILED'
    if examples_match and len(results) == 0:
        results['_rspec_tests_run_'] = 'PASSED'

    return results


def parse_junit_summary_output(output: str) -> Dict[str, str]:
    """Parse junit summary test output."""
    results = {}
    clean_output = strip_ansi(output)

    # Individual test failures: ClassName.testName(ExceptionClass): message
    for line in clean_output.split('\n'):
        line_stripped = line.strip()
        # JUnit format: test_name(org.junit.ExpectedException): expected:...
        match = re.match(r'^(\d+\)\s+)?(\S+(?:\.\S+)?)\([^)]*Exception[^)]*\)\s*:', line_stripped)
        if match:
            test_name = match.group(2).strip()
            if test_name:
                results[test_name] = 'FAILED'
                continue
        # JUnit format: test_name(org.junit.ComparisonFailure): expected:...
        match = re.match(r'^(\d+\)\s+)?(\S+(?:\.\S+)?)\([^)]*ComparisonFailure[^)]*\)\s*:', line_stripped)
        if match:
            test_name = match.group(2).strip()
            if test_name:
                results[test_name] = 'FAILED'
                continue

    # Summary: Tests run: N, Failures: M
    summary_match = re.search(r'Tests run:\s*(\d+),\s*Failures:\s*(\d+)(?:,\s*Errors:\s*(\d+))?(?:,\s*Skipped:\s*(\d+))?', clean_output)
    if summary_match:
        failures = int(summary_match.group(2))
        errors = int(summary_match.group(3) or 0)
        if failures > 0 and len(results) == 0:
            results['_junit_tests_failed_'] = 'FAILED'
        elif errors > 0 and len(results) == 0:
            results['_junit_tests_failed_'] = 'FAILED'

    return results


def parse_python_unittest_output(output: str) -> Dict[str, str]:
    """Parse python unittest test output."""
    results = {}
    clean_output = strip_ansi(output)

    for line in clean_output.split('\n'):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # test_name (module.Class) ... ok
        match = re.match(r'^(.+?)\s+\([^)]+\)\s+\.\.\.\s+ok\s*$', line_stripped)
        if match:
            results[match.group(1).strip()] = 'PASSED'
            continue

        # test_name (module.Class) ... FAIL
        match = re.match(r'^(.+?)\s+\([^)]+\)\s+\.\.\.\s+FAIL\s*$', line_stripped)
        if match:
            results[match.group(1).strip()] = 'FAILED'
            continue

        # test_name (module.Class) ... ERROR
        match = re.match(r'^(.+?)\s+\([^)]+\)\s+\.\.\.\s+ERROR\s*$', line_stripped)
        if match:
            results[match.group(1).strip()] = 'FAILED'
            continue

        # test_name (module.Class) ... skipped
        match = re.match(r'^(.+?)\s+\([^)]+\)\s+\.\.\.\s+skipped\s*', line_stripped, re.IGNORECASE)
        if match:
            results[match.group(1).strip()] = 'PASSED'
            continue

        # test_name (module.Class) ... expected failure
        match = re.match(r'^(.+?)\s+\([^)]+\)\s+\.\.\.\s+expected\s+failure\s*$', line_stripped, re.IGNORECASE)
        if match:
            results[match.group(1).strip()] = 'PASSED'
            continue

    return results


def parse_s2n_output(output: str) -> Dict[str, str]:
    """Parse s2n test output."""
    results = {}
    clean_output = strip_ansi(output)

    for line in clean_output.split('\n'):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # PASSED N tests
        match = re.match(r'Running\s+\S+\s+\.\.\.\s+PASSED\s+(\d+)\s+tests?', line_stripped)
        if match:
            test_name = line_stripped.split('...')[0].replace('Running', '').strip()
            results[test_name] = 'PASSED'
            continue

        # FAILED test N
        match = re.match(r'FAILED\s+test\s+(\d+)', line_stripped)
        if match:
            results[f'test_{match.group(1)}'] = 'FAILED'
            continue

        # Running ... with FAILED or error
        match = re.match(r'Running\s+(\S+)\s+\.\.\.\s+FAILED', line_stripped)
        if match:
            results[match.group(1)] = 'FAILED'
            continue

    return results


def parse_fluentbit_output(output: str) -> Dict[str, str]:
    """Parse fluentbit test output."""
    results = {}
    clean_output = strip_ansi(output)

    for line in clean_output.split('\n'):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # Test name... [ OK ] or [ FAILED ]
        match = re.match(r'^Test\s+(.+?)\s*\.\.\.\s*\[\s*(OK|FAILED|PASS|SKIP)\s*\]', line_stripped)
        if match:
            results[match.group(1).strip()] = 'PASSED' if match.group(2).strip() in ('OK', 'PASS') else 'FAILED'
            continue

        # check_ style: filter_stdout.c:33: Check filter_ffd >= 0... failed
        match = re.match(r'^\s*\S+:\d+:\s+Check\s+.+?\.\.\.\s+(ok|failed|FAILED|OK)\s*$', line_stripped)
        if match:
            status = match.group(1).strip().lower()
            # Extract a meaningful name from the check line
            check_match = re.match(r'^\s*(\S+):(\d+):\s+Check\s+(.+?)\.\.\.', line_stripped)
            if check_match:
                name = f"{check_match.group(1)}:{check_match.group(2)}"
                results[name] = 'PASSED' if status == 'ok' else 'FAILED'

    return results


@_strip_ansi_input
def parse_minitest_output(output: str) -> Dict[str, str]:
    """Parse minitest test output."""
    results = {}

    def _norm_name(name: str) -> str:
        """Normalize test name: replace '. ' (sed artifact) with '#' (canonical),
        and strip leading progress dots from verbose output."""
        if name.startswith('.') and '#' in name:
            name = name.lstrip('.')
        return name.replace('. ', '#', 1) if '. ' in name else name

    clean_output = strip_ansi(output)

    lines = clean_output.split('\n')
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # Verbose format 2: Class. test_name TIME = RESULT (e.g. "0.46 = ." or "0.44 = F")
        match = re.match(r'^(.+(?:#|\.\s)\S+)\s+[\d.]+\s*=\s*\.\s*$', line_stripped)
        if match:
            results[_norm_name(match.group(1).strip())] = 'PASSED'
            continue

        match = re.match(r'^(.+(?:#|\.\s)\S+)\s+[\d.]+\s*=\s*F\s*$', line_stripped)
        if match:
            results[_norm_name(match.group(1).strip())] = 'FAILED'
            continue

        match = re.match(r'^(.+(?:#|\.\s)\S+)\s+[\d.]+\s*=\s*E\s*$', line_stripped)
        if match:
            results[_norm_name(match.group(1).strip())] = 'ERROR'
            continue

        # Verbose format 1: Class#test_method = 0.12 s (original format, no sed)
        match = re.match(r'^(.+(?:#|\.\s)[^\s=]+)\s*=\s*[\d.]+\s*s\s*$', line_stripped)
        if match:
            results[_norm_name(match.group(1).strip())] = 'PASSED'
            continue

        # Verbose format 1: Class#test_method = F (short form)
        match = re.match(r'^(.+(?:#|\.\s)[^\s=]+)\s*=\s*F\s*$', line_stripped)
        if match:
            results[_norm_name(match.group(1).strip())] = 'FAILED'
            continue

        # Verbose format 1: Class#test_method = E (error, short form)
        match = re.match(r'^(.+(?:#|\.\s)[^\s=]+)\s*=\s*E\s*$', line_stripped)
        if match:
            results[_norm_name(match.group(1).strip())] = 'ERROR'
            continue

        # Verbose format 3: Class#test_method = 0.12 s = . (double equals, pass)
        match = re.match(r'^(.+(?:#|\.\s)[^\s=]+)\s*=\s*[\d.]+\s*s\s*=\s*\.\s*$', line_stripped)
        if match:
            results[_norm_name(match.group(1).strip())] = 'PASSED'
            continue

        # Verbose format 3: Class#test_method = 0.12 s = F (double equals, fail)
        match = re.match(r'^(.+(?:#|\.\s)[^\s=]+)\s*=\s*[\d.]+\s*s\s*=\s*F\s*$', line_stripped)
        if match:
            results[_norm_name(match.group(1).strip())] = 'FAILED'
            continue

        # Verbose format 3: Class#test_method = 0.12 s = E (double equals, error)
        match = re.match(r'^(.+(?:#|\.\s)[^\s=]+)\s*=\s*[\d.]+\s*s\s*=\s*E\s*$', line_stripped)
        if match:
            results[_norm_name(match.group(1).strip())] = 'ERROR'
            continue

        # Flexible verbose formats: handle test names with spaces after # separator
        # (e.g., Rails Minitest verbose: "TestClass#test_0001_does not share the same array = 0.00 s = F")
        # These patterns match the distinctive suffix to capture the full test name.
        # Guard: require '#' in the captured name to avoid false positives on unrelated lines.

        # Flex format 3: ClassName#test description = 0.00 s = . (or F/E)
        match = re.match(r'^(.+)\s+=\s+[\d.]+\s+s\s+=\s*([.FE])\s*$', line_stripped)
        if match and '#' in match.group(1):
            name = match.group(1).strip()
            status_char = match.group(2)
            results[_norm_name(name)] = 'FAILED' if status_char == 'F' else ('ERROR' if status_char == 'E' else 'PASSED')
            continue

        # Flex format 1: ClassName#test description = 0.12 s
        match = re.match(r'^(.+)\s+=\s+[\d.]+\s+s\s*$', line_stripped)
        if match and '#' in match.group(1):
            name = match.group(1).strip()
            results[_norm_name(name)] = 'PASSED'
            continue

        # Flex format 1 short: ClassName#test description = F (or E)
        match = re.match(r'^(.+)\s+=\s*([FE])\s*$', line_stripped)
        if match and '#' in match.group(1):
            name = match.group(1).strip()
            status_char = match.group(2)
            results[_norm_name(name)] = 'FAILED' if status_char == 'F' else 'ERROR'
            continue

        # Minitest spec-style (fluentd format):
        #   test: . <description>:\t.: (time) → PASSED  (also handles .method)
        #   test: <description>:\tF → FAILED
        #   test: <description>:\tE → ERROR
        # Note: failure output may append (ClassName) to spec-style names, e.g.
        #   "test: desc(ClassName):\tF" → strip (ClassName) to match pass output "test: desc"
        match = re.match(r'^test:\s+(.+?):\s*\.:\s*\(', line_stripped)
        if match:
            name = f'test: {match.group(1).strip()}'
            # Strip (ClassName) suffix from spec-style names
            name = re.sub(r'\([^)]+\)$', '', name).strip()
            if name not in results:
                results[name] = 'PASSED'
            continue
        match = re.match(r'^test:\s+(.+?):\s+F\s*$', line_stripped)
        if match:
            name = f'test: {match.group(1).strip()}'
            # Strip (ClassName) suffix from spec-style names
            name = re.sub(r'\([^)]+\)$', '', name).strip()
            # Don't overwrite if already captured from Failure detail with class name
            if name not in results:
                results[name] = 'FAILED'
            continue
        match = re.match(r'^test:\s+(.+?):\s+E\s*$', line_stripped)
        if match:
            name = f'test: {match.group(1).strip()}'
            # Strip (ClassName) suffix from spec-style names
            name = re.sub(r'\([^)]+\)$', '', name).strip()
            if name not in results:
                results[name] = 'ERROR'
            continue

        # Minitest method-style: test_name(ClassName):\tF/\tE/\t.: (time)
        # e.g. "test_equal(IntailPositionFileTest::TargetInfo):\t\tF"
        # e.g. "test_not_equal:\t\t.: (0.000120)"
        # e.g. "test_parser_engine[normal]:\t.: (0.000645)" — parameterized tests use [param]
        # Method names may contain ? or !: test_eql?, test_save!
        match = re.match(r'^(test_[\w?!]+(?:\[[^\]]*\]|\([^)]*\))?):\s+\.:\s*\(', line_stripped)
        if match:
            results[match.group(1)] = 'PASSED'
            continue
        match = re.match(r'^(test_[\w?!]+(?:\[[^\]]*\]|\([^)]*\))?):\s+F\s*$', line_stripped)
        if match:
            results[match.group(1)] = 'FAILED'
            continue
        match = re.match(r'^(test_[\w?!]+(?:\[[^\]]*\]|\([^)]*\))?):\s+E\s*$', line_stripped)
        if match:
            results[match.group(1)] = 'ERROR'
            continue

        # Numbered failure: "  1) Failure:" followed by test name on next line
        match = re.match(r'^\d+\)\s+Failure:', line_stripped)
        if match and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            name_match = re.match(r'^(.+(?:#|\.\s)\S+?)(?:\s+\[|:)', next_line)
            if name_match:
                results[_norm_name(name_match.group(1).strip())] = 'FAILED'
            continue

        # Numbered error: "  N) Error:" followed by test name on next line
        match = re.match(r'^\d+\)\s+Error:', line_stripped)
        if match and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            name_match = re.match(r'^(.+(?:#|\.\s)\S+?)(?:\s+\[|:)', next_line)
            if name_match:
                results[_norm_name(name_match.group(1).strip())] = 'ERROR'
            continue

        # Unnumbered failure: "Failure:" on its own line, test name on next line
        if re.match(r'^Failure:\s*$', line_stripped) and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            name_match = re.match(r'^(.+(?:#|\.\s)\S+?)(?:\s+\[|:)', next_line)
            if name_match:
                results[_norm_name(name_match.group(1).strip())] = 'FAILED'
            continue
        # Failure with test_name(ClassName) on same line: "Failure: test_equal(ClassName::context)"
        match = re.match(r'^Failure:\s+(test_[\w?!]+\([^)]+\))', line_stripped)
        if match:
            results[match.group(1)] = 'FAILED'
            continue
        # Failure with test: <name>(ClassName) on same line
        match = re.match(r'^Failure:\s+test:\s+(.+?\([^)]+\))\s*:', line_stripped)
        if match:
            # Strip (ClassName) suffix to match spec-style pass output
            name = re.sub(r'\([^)]+\)$', '', match.group(1).strip()).strip()
            results[f'test: {name}'] = 'FAILED'
            continue
        match = re.match(r'^Failure:\s+(.+?(?:#|\.\s)\S+?)\s*:', line_stripped)
        if match:
            results[_norm_name(match.group(1).strip())] = 'FAILED'
            continue
        match = re.match(r'^Failure:\s*(\S+)', line_stripped)
        if match:
            results[match.group(1).strip()] = 'FAILED'
            continue

        # Unnumbered error: "Error:" on its own line, test name on next line
        if re.match(r'^Error:\s*$', line_stripped) and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            name_match = re.match(r'^(.+(?:#|\.\s)\S+?)(?:\s+\[|:)', next_line)
            if name_match:
                name = name_match.group(1).strip()
                results[_norm_name(name)] = 'ERROR'
            continue
        match = re.match(r'^Error:\s+test:\s+(.+?\([^)]+\))\s*:', line_stripped)
        if match:
            results[f'test: {match.group(1).strip()}'] = 'ERROR'
            continue
        match = re.match(r'^Error:\s*(\S+)', line_stripped)
        if match:
            results[match.group(1).strip()] = 'ERROR'
            continue

    # Summary: X runs, Y assertions, Z failures, W errors
    runs_match = re.search(r'(\d+)\s+runs?', clean_output, re.IGNORECASE)
    failures_match = re.search(r'(\d+)\s+failures?', clean_output, re.IGNORECASE)
    errors_match = re.search(r'(\d+)\s+errors?', clean_output, re.IGNORECASE)

    if runs_match:
        total = int(runs_match.group(1))
        failures = int(failures_match.group(1)) if failures_match else 0
        errors = int(errors_match.group(1)) if errors_match else 0
        passed = total - failures - errors
        if failures > 0 or errors > 0:
            if len(results) == 0:
                results['_minitest_failed_'] = 'FAILED'
        if passed > 0 and len(results) == 0:
            results['_minitest_passed_'] = 'PASSED'

    return results


@_strip_ansi_input
def parse_ruby_test_unit_output(output: str) -> Dict[str, str]:
    """Parse ruby test unit test output."""
    results = {}
    clean_output = strip_ansi(output)

    for line in clean_output.split('\n'):
        line_stripped = line.strip()

        # test_name: .: (time) — passed with dot indicator
        match = re.match(r'^(test_\w+)\s*:\s*\.\s*:\s*\([\d.]+\s*s?\)\s*$', line_stripped)
        if match:
            results[match.group(1)] = 'PASSED'
            continue
        # test_name: F — failed
        match = re.match(r'^(test_\w+)\s*:\s*F\s*$', line_stripped)
        if match:
            results[match.group(1)] = 'FAILED'
            continue
        # test_name: E — error
        match = re.match(r'^(test_\w+)\s*:\s*E\s*$', line_stripped)
        if match:
            results[match.group(1)] = 'ERROR'
            continue
        # test_name: . — passed (single dot after colon, no time)
        match = re.match(r'^(test_\w+)\s*:\s*\.\s*$', line_stripped)
        if match:
            results[match.group(1)] = 'PASSED'
            continue
        # Loaded suite / Started — skip
        if line_stripped.startswith('Loaded suite') or line_stripped == 'Started':
            continue
        # Class header: ClassName: — skip
        if re.match(r'^\w[\w:]*:\s*$', line_stripped):
            continue

    # Summary: N tests, M assertions, X failures, Y errors, ...
    # This format handles both test/unit and minitest-style summaries
    tests_match = re.search(r'(\d+)\s+tests?\s*,\s*(\d+)\s+assertions?\s*,\s*(\d+)\s+failures?\s*,\s*(\d+)\s+errors?',
                            clean_output, re.IGNORECASE)
    if tests_match:
        total = int(tests_match.group(1))
        failures = int(tests_match.group(3))
        errors = int(tests_match.group(4))
        if failures > 0 or errors > 0:
            if not any(v == 'FAILED' for v in results.values()):
                results['_ruby_unit_failed_'] = 'FAILED'
        passed = total - failures - errors
        if passed > 0 and not any(v == 'PASSED' for v in results.values()):
            results['_ruby_unit_passed_'] = 'PASSED'

    return results


def parse_micropython_output(output: str) -> Dict[str, str]:
    """Parse micropython test output."""
    results = {}
    clean_output = strip_ansi(output)

    for line in clean_output.split('\n'):
        line_stripped = line.strip()
        # pass test_name
        if line_stripped.startswith('pass '):
            test_name = line_stripped[len('pass '):].strip()
            if test_name:
                results[test_name] = 'PASSED'
        # FAIL test_name
        elif line_stripped.startswith('FAIL '):
            test_name = line_stripped[len('FAIL '):].strip()
            if test_name:
                results[test_name] = 'FAILED'
        # skip test_name
        elif line_stripped.startswith('skip '):
            test_name = line_stripped[len('skip '):].strip()
            if test_name:
                results[test_name] = 'SKIPPED'

    return results


def parse_serenityos_test_output(output: str) -> Dict[str, str]:
    """Parse serenityos test test output."""
    results = {}
    clean_output = strip_ansi(output)

    # Track currently running test
    current_test = None

    for line in clean_output.split('\n'):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # Running test 'name'.
        match = re.match(r"^Running test '([^']+)'", line_stripped)
        if match:
            current_test = match.group(1)
            continue

        # Completed test 'name' in Xms
        match = re.match(r"^Completed test '([^']+)'", line_stripped)
        if match:
            results[match.group(1)] = 'PASSED'
            current_test = None
            continue

        # Failed test 'name' in Xms
        match = re.match(r"^Failed test '([^']+)'", line_stripped)
        if match:
            results[match.group(1)] = 'FAILED'
            current_test = None
            continue

    # Summary fallback: if we have "Out of N tests, X passed and Y failed" but no parsed tests
    summary_match = re.search(r'Out of (\d+) tests?, (\d+) passed and (\d+) failed', clean_output)
    if summary_match and not results:
        total = int(summary_match.group(1))
        passed = int(summary_match.group(2))
        failed = int(summary_match.group(3))
        if failed > 0:
            results['_serenityos_failed_'] = 'FAILED'
        if passed > 0:
            results['_serenityos_passed_'] = 'PASSED'

    return results


def parse_serenityos_js_test_output(output: str) -> Dict[str, str]:
    """Parse serenityos js test test output."""
    results = {}
    clean_output = strip_ansi(output)

    for line in clean_output.split('\n'):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # PASS test-name.js (Xms) — may have leading ANSI whitespace
        match = re.match(r'^PASS\s+(\S+)', line_stripped)
        if match:
            results[match.group(1)] = 'PASSED'
            continue

        # FAIL test-name.js (Xms)
        match = re.match(r'^FAIL\s+(\S+)', line_stripped)
        if match:
            results[match.group(1)] = 'FAILED'
            continue

    # Also try to extract test name from the ❌ Suite + Test lines
    if not results:
        suite_match = re.search(r'Suite:\s+(.+)', clean_output)
        test_match = re.search(r'Test:\s+(\S+)', clean_output)
        if suite_match and test_match:
            suite = suite_match.group(1).strip()
            test = test_match.group(1).strip()
            results[f'{suite}/{test}'] = 'FAILED'

    return results


def parse_headless_browser_test_output(output: str) -> Dict[str, str]:
    """Parse headless browser test test output."""
    results = {}
    clean_output = strip_ansi(output)

    # Track test names from numbered list
    test_names = []

    for line in clean_output.split('\n'):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # N/M: path/to/test-name.html (or .txt, .js, etc.)
        match = re.match(r'^\d+/\d+:\s+(.+)$', line_stripped)
        if match:
            test_path = match.group(1).strip()
            # Extract filename from path like "Layout/input/name.html" or "Text/input/name.html"
            # Use the full relative path as the test name for uniqueness
            name = test_path
            test_names.append(name)
            continue

        # Test PASSED: test-name output matches expected
        match = re.match(r'^Test PASSED:\s+(\S+)', line_stripped)
        if match:
            results[match.group(1)] = 'PASSED'
            continue

        # Test FAILED: test-name output does not match expected
        match = re.match(r'^Test FAILED:\s+(\S+)', line_stripped)
        if match:
            results[match.group(1)] = 'FAILED'
            continue

    # If we have numbered test names but no PASSED/FAILED, use the Fail: line + Pass/Fail summary
    if test_names and not results:
        fail_line = re.search(r'^Fail:\s+(.+)$', clean_output, re.MULTILINE)
        failed_name = fail_line.group(1).strip() if fail_line else None

        summary = re.search(r'Pass:\s*(\d+),\s*Fail:\s*(\d+),\s*Timeout:\s*(\d+)', clean_output)
        if summary:
            fail_count = int(summary.group(2))
            pass_count = int(summary.group(1))
        else:
            fail_count = 1
            pass_count = len(test_names) - 1

        # Try to match fail_name to test_names list
        if failed_name:
            # failed_name may include a project-relative test file name.
            # test_names are like "Layout/input/cell-auto-max-width-table-percentage-width.html"
            matched_fail = None
            for tn in test_names:
                if tn in failed_name or failed_name.endswith(tn.split('/')[-1]):
                    matched_fail = tn
                    break
            if matched_fail:
                results[matched_fail] = 'FAILED'
                for tn in test_names:
                    if tn != matched_fail:
                        results[tn] = 'PASSED'
            else:
                # Can't match — use the filename as test name
                short_name = failed_name.split('/')[-1].replace('.html', '').replace('.txt', '')
                results[short_name] = 'FAILED'
                if pass_count > 0:
                    results['_headless_browser_passed_'] = 'PASSED'
        elif fail_count == 0 and pass_count > 0:
            # All tests passed — mark all numbered test names as PASSED
            for tn in test_names:
                results[tn] = 'PASSED'
        elif fail_count > 0:
            results['_headless_browser_failed_'] = 'FAILED'

    return results


def is_valid_test_name(name: str) -> bool:
    """Is valid test name."""
    if not name or len(name) < 2:
        return False

    
    if len(name) > 200:
        return False

    
    if '\n' in name or '\r' in name or '\\n' in name or '\\u000a' in name:
        return False

    
    # natural-language test descriptions like "should return value" or "uses correct class name"
    code_start_patterns = ['const ', 'function ', 'return ', 'import ', 'export ',
                           'class ', 'interface ', 'type ', 'let ', 'var ', 'void ']
    if any(name.startswith(p) for p in code_start_patterns):
        return False

    
    if name.startswith('$') and ('->' in name or '::' in name or '=' in name):
        return False

    
    if name.startswith(('cd ', 'git ', 'yarn ', 'npm ', 'pip ', 'source ', 'export ',
                        'conda ', 'echo ', 'cat ', 'ls ', 'mkdir ', 'rm ', 'cp ', 'mv ',
                        'Checking patch ', 'Applied patch ')):
        return False

    
    if name.startswith(('+', '++', '+++')):
        return False

    
    if name.startswith(('OMNIGRIL_', 'rc=', 'rc1=', 'rc2=')):
        return False

    
    if '[build failed]' in name or '[compile failed]' in name:
        return False

    
    if name.startswith('_') and ('summary' in name or 'progress' in name or 'tests_' in name):
        return True

    
    if name.startswith('_') and not name.startswith('test'):
        
        if 'summary' in name.lower() or 'tests_' in name.lower():
            return False

    return True


def clean_test_results(results: Dict[str, str]) -> Dict[str, str]:
    """Clean test results."""
    return {k: v for k, v in results.items() if is_valid_test_name(k)}


def extract_summary_counts(results: Dict[str, str], output: str) -> tuple:
    """Extract summary counts."""
    if not output or output.strip() == '':
        return (0, 0)

    
    clean = strip_ansi(output)

    # Truncate to last 10KB — test summaries are always at the end of output.
    # Full logs (up to 4MB) cause catastrophic backtracking on regexes with .*? + DOTALL.
    if len(clean) > 10000:
        clean = clean[-10000:]

    clean_lower = clean.lower()

    

    
    if 'FAIL!' in clean and 'Passed' in clean and 'Failed' in clean:
        m = re.search(r'FAIL!.*?(\d+)\s+Passed.*?(\d+)\s+Failed', clean, re.IGNORECASE)
        if m:
            return (int(m.group(2)), int(m.group(1)))

    # Go test / gotestsum: DONE X tests, Y failures in Zs
    if 'DONE' in clean and 'tests' in clean_lower:
        m = re.search(r'DONE\s+(\d+)\s+tests?\s*,\s*(\d+)\s+failures?', clean, re.IGNORECASE)
        if m:
            total = int(m.group(1))
            failed = int(m.group(2))
            return (failed, total - failed)

    # Elixir: X tests, Y failures
    if re.search(r'\d+\s+tests?\s*,\s*\d+\s+failures?', clean, re.IGNORECASE):
        m = re.search(r'(\d+)\s+tests?\s*,\s*(\d+)\s+failures?', clean, re.IGNORECASE)
        if m:
            total = int(m.group(1))
            failed = int(m.group(2))
            return (failed, total - failed)

    # Jest: Tests: X failed, [Y todo,] Z passed
    
    if 'Tests:' in clean and 'failed' in clean_lower and 'passed' in clean_lower:
        m = re.search(r'Tests:\s*(\d+)\s+failed\b.*?(\d+)\s+passed', clean, re.IGNORECASE | re.DOTALL)
        if m:
            return (int(m.group(1)), int(m.group(2)))

    # Mocha: X passing, Y failing
    if 'passing' in clean_lower and 'failing' in clean_lower:
        m = re.search(r'(\d+)\s+passing.*?(\d+)\s+failing', clean, re.IGNORECASE | re.DOTALL)
        if m:
            return (int(m.group(2)), int(m.group(1)))

    # Karma: Executed N of M (X FAILED) or Executed N of M SUCCESS
    if re.search(r'Executed\s+\d+\s+of\s+\d+', clean, re.IGNORECASE):
        m = re.search(r'Executed\s+\d+\s+of\s+(\d+)\s+\((\d+)\s+FAILED\)', clean, re.IGNORECASE)
        if m:
            total = int(m.group(1))
            failed = int(m.group(2))
            return (failed, total - failed)
        m = re.search(r'Executed\s+(\d+)\s+of\s+(\d+)\s+SUCCESS', clean, re.IGNORECASE)
        if m:
            total = int(m.group(2))
            return (0, total)

    # Vitest: Tests X failed | Y passed
    if re.search(r'Tests\s+\d+\s+failed', clean, re.IGNORECASE):
        m = re.search(r'Tests\s+(\d+)\s+failed.*?(\d+)\s+passed', clean, re.IGNORECASE)
        if m:
            return (int(m.group(1)), int(m.group(2)))

    # Vitest: Test Files X failed | Y passed
    if re.search(r'Test Files\s+\d+\s+failed', clean, re.IGNORECASE):
        m = re.search(r'Test Files\s+(\d+)\s+failed.*?(\d+)\s+passed', clean, re.IGNORECASE)
        if m:
            return (int(m.group(1)), int(m.group(2)))

    # Ginkgo: Ginkgo ran X suite in Ys
    if 'Ginkgo ran' in clean:
        # FAIL! -- X Passed | Y Failed
        m = re.search(r'(\d+)\s+Passed.*?(\d+)\s+Failed', clean, re.IGNORECASE)
        if m:
            return (int(m.group(2)), int(m.group(1)))
        # SUCCESS! -- X Passed | Y Failed | Z Pending
        m = re.search(r'SUCCESS!.*?(\d+)\s+Passed', clean, re.IGNORECASE)
        if m:
            return (0, int(m.group(1)))

    # Google Test: [  PASSED  ] X tests. [  FAILED  ] Y tests
    if re.search(r'\[\s*PASSED\s*\]', clean) or re.search(r'\[\s*FAILED\s*\]', clean):
        passed_m = re.search(r'\[\s*PASSED\s*\]\s*(\d+)\s+tests?', clean, re.IGNORECASE)
        failed_m = re.search(r'\[\s*FAILED\s*\]\s*(\d+)\s+tests?', clean, re.IGNORECASE)
        passed = int(passed_m.group(1)) if passed_m else 0
        failed = int(failed_m.group(1)) if failed_m else 0
        if passed > 0 or failed > 0:
            return (failed, passed)

    # xUnit.net: Failed! - Failed: X, Passed: Y, Skipped: Z, Total: N
    if re.search(r'Failed!\s+-\s+Failed:', clean, re.IGNORECASE):
        m = re.search(r'Failed:\s*(\d+).*Passed:\s*(\d+).*Total:\s*(\d+)', clean, re.IGNORECASE)
        if m:
            return (int(m.group(1)), int(m.group(2)))

    # Web Test Runner: Chromium: |████| 1/1 test files | 8 passed, 3 failed
    if re.search(r'(?:Chromium|Firefox|Webkit|Safari|Chrome):\s*\|.*\d+\s+passed', clean, re.IGNORECASE):
        m = re.search(r'(\d+)\s+passed,\s*(\d+)\s+failed', clean, re.IGNORECASE)
        if m:
            return (int(m.group(2)), int(m.group(1)))

    # Go testify: Tests: X, Assertions: Y, Failures: Z
    if re.search(r'Tests:\s*\d+,\s*Assertions:\s*\d+,\s*Failures?:\s*\d+', clean):
        m = re.search(r'Tests:\s*(\d+).*Failures?:\s*(\d+)', clean, re.IGNORECASE)
        if m:
            total = int(m.group(1))
            failed = int(m.group(2))
            return (failed, total - failed)

    # .NET: Passed: X, Failed: Y
    if 'Passed:' in clean and 'Failed:' in clean:
        m = re.search(r'Passed:\s*(\d+).*?Failed:\s*(\d+)', clean, re.IGNORECASE | re.DOTALL)
        if m:
            return (int(m.group(2)), int(m.group(1)))

    # Rust: X passed; Y failed
    if 'passed;' in clean_lower and 'failed' in clean_lower:
        m = re.search(r'(\d+)\s+passed;\s*(\d+)\s+failed', clean, re.IGNORECASE)
        if m:
            return (int(m.group(2)), int(m.group(1)))

    
    if 'passed' in clean_lower and 'failed' in clean_lower:
        matches = re.findall(r'(\d+)\s+passed.*?(\d+)\s+failed', clean, re.IGNORECASE)
        if matches:
            valid = [(int(p), int(f)) for p, f in matches if int(p) > 0 or int(f) > 0]
            if valid:
                passed, failed = valid[-1]
                return (failed, passed)

    # Django/unittest: Ran X tests in Ys
    if re.search(r'Ran\s+\d+\s+test', clean, re.IGNORECASE):
        if ('OK' in clean or 'FAILED' in clean) and 'Ran all test suites' not in clean:
            m = re.search(r'Ran\s+(\d+)\s+test', clean, re.IGNORECASE)
            if m:
                total = int(m.group(1))
                failed = 0
                fm = re.search(r'failures?=(\d+)', clean, re.IGNORECASE)
                em = re.search(r'errors?=(\d+)', clean, re.IGNORECASE)
                if fm: failed += int(fm.group(1))
                if em: failed += int(em.group(1))
                return (failed, total - failed)

    # pytest: = X failed, Y passed / X errors, Y passed =
    if re.search(r'=\s*\d+\s+(failed|passed|error)', clean, re.IGNORECASE):
        m = re.search(r'=\s*(\d+)\s+failed.*?(\d+)\s+passed', clean, re.IGNORECASE | re.DOTALL)
        if m:
            failed = int(m.group(1))
            passed = int(m.group(2))
            
            em = re.search(r'(\d+)\s+errors?', clean, re.IGNORECASE)
            if em:
                failed += int(em.group(1))
            return (failed, passed)
        
        m_err = re.search(r'(\d+)\s+errors?.*?(\d+)\s+passed', clean, re.IGNORECASE | re.DOTALL)
        if m_err:
            return (int(m_err.group(1)), int(m_err.group(2)))
        
        m_err_only = re.search(r'=\s*(\d+)\s+errors?\s+in\s+[\d.]+s\s*=', clean, re.IGNORECASE)
        if m_err_only:
            return (int(m_err_only.group(1)), 0)
        
        m = re.search(r'=\s*(\d+)\s+passed\s*=', clean, re.IGNORECASE)
        if m:
            return (0, int(m.group(1)))

    
    if 'failed' in clean_lower and 'passed' in clean_lower and 'Tests:' not in clean:
        for pat in [re.compile(r'(\d+)\s+failed.*?(\d+)\s+passed', re.IGNORECASE),
                    re.compile(r'(\d+)\s+passed.*?(\d+)\s+failed', re.IGNORECASE)]:
            m = pat.search(clean)
            if m:
                if 'failed' in m.group(0).lower()[:20]:
                    return (int(m.group(1)), int(m.group(2)))
                else:
                    return (int(m.group(2)), int(m.group(1)))

    
    go_summary = re.search(r'(\d+)\s+passed[,\s]+(\d+)\s+failed', clean, re.IGNORECASE)
    if go_summary:
        return (int(go_summary.group(2)), int(go_summary.group(1)))

    
    pass_count = len(re.findall(r'--- PASS', clean))
    fail_count = len(re.findall(r'--- FAIL', clean))
    if pass_count > 0 or fail_count > 0:
        return (fail_count, pass_count)

    # Go test: FAIL\tpkgname or ok pkgname (package-level results without -v)
    go_fail_pkgs = len(re.findall(r'^FAIL\s+[\w/\.\-]+', clean, re.MULTILINE))
    go_ok_pkgs = len(re.findall(r'^ok\s+[\w/\.\-]+', clean, re.MULTILINE))
    go_pass_pkgs = len(re.findall(r'^PASS\s+[\w/\.\-]+', clean, re.MULTILINE))
    if go_fail_pkgs > 0 or go_ok_pkgs > 0 or go_pass_pkgs > 0:
        # Subtract build failures from fail count
        build_fails = len(re.findall(r'\[build failed\]', clean))
        actual_fail = max(0, go_fail_pkgs - build_fails)
        actual_pass = go_ok_pkgs + go_pass_pkgs
        if actual_fail > 0 or actual_pass > 0:
            return (actual_fail, actual_pass)

    # Go test: OOPS: N passed, M FAILED or OK: N passed
    oops_match = re.search(r'OOPS:\s*(\d+)\s+passed(?:,\s*(\d+)\s+FAILED)?', clean, re.IGNORECASE)
    if oops_match:
        passed = int(oops_match.group(1))
        failed = int(oops_match.group(2) or 0)
        return (failed, passed)
    ok_match = re.search(r'\bOK:\s*(\d+)\s+passed\b', clean, re.IGNORECASE)
    if ok_match:
        return (0, int(ok_match.group(1)))

    # Gradle: Gradle Test Run :module:test Results: FAILED (128 tests, 125 successes, 3 failures)
    gradle_match = re.search(
        r'Gradle Test Run\s+\S+\s+Results:\s+(?:SUCCESS|FAILED)\s+\((\d+)\s+tests?,\s+(\d+)\s+successes?,\s+(\d+)\s+failures?',
        clean, re.IGNORECASE
    )
    if gradle_match:
        failed = int(gradle_match.group(3))
        passed = int(gradle_match.group(2))
        return (failed, passed)

    # PHPUnit: Tests: X, Assertions: Y, Failures: Z, Errors: W
    if re.search(r'Tests:\s*\d+.*?Assertions:', clean, re.IGNORECASE):
        m = re.search(r'Tests:[ \t]*(\d+)', clean, re.IGNORECASE)
        if m:
            total = int(m.group(1))
            errors = failures = 0
            em = re.search(r'Errors:[ \t]*(\d+)', clean, re.IGNORECASE)
            fm = re.search(r'Failures:[ \t]*(\d+)', clean, re.IGNORECASE)
            if em: errors = int(em.group(1))
            if fm: failures = int(fm.group(1))
            return (errors + failures, total - errors - failures)

    
    if '# pass' in clean_lower or '# fail' in clean_lower:
        pass_m = re.search(r'#\s*pass\s+(\d+)', clean, re.IGNORECASE)
        fail_m = re.search(r'#\s*fail\s+(\d+)', clean, re.IGNORECASE)
        if pass_m or fail_m:
            passed = int(pass_m.group(1)) if pass_m else 0
            failed = int(fail_m.group(1)) if fail_m else 0
            return (failed, passed)

    # PHPUnit: OK (N tests, M assertions)
    phpunit_ok = re.search(r'\bOK\s*\(\s*(\d+)\s+tests?', clean, re.IGNORECASE)
    if phpunit_ok:
        return (0, int(phpunit_ok.group(1)))

    # TAP: # failed X of Y tests
    if '# failed' in clean_lower:
        m = re.search(r'#\s*failed\s+(\d+)\s+of\s+(\d+)\s+tests', clean, re.IGNORECASE)
        if m:
            return (int(m.group(1)), int(m.group(2)) - int(m.group(1)))

    # pytest: X passed in Ys
    if 'passed' in clean_lower and 'failed' not in clean_lower:
        m = re.search(r'(\d+)\s+passed\s+in\s+[\d.]+s', clean, re.IGNORECASE)
        if m:
            return (0, int(m.group(1)))

    
    dart_summary = re.search(r'\d+:\d+\s+\+(\d+)\s+-(\d+)', clean)
    if dart_summary:
        return (int(dart_summary.group(2)), int(dart_summary.group(1)))

    # AVA / generic: "N tests failed" / "N tests passed" (standalone summary lines)
    ava_fail = re.search(r'^\s*(\d+)\s+tests?\s+failed\s*$', clean, re.MULTILINE | re.IGNORECASE)
    ava_pass = re.search(r'^\s*(\d+)\s+tests?\s+passed\s*$', clean, re.MULTILINE | re.IGNORECASE)
    if ava_fail or ava_pass:
        failed = int(ava_fail.group(1)) if ava_fail else 0
        passed = int(ava_pass.group(1)) if ava_pass else 0
        if failed > 0 or passed > 0:
            return (failed, passed)

    
    if 'Some tests failed' in clean:
        m = re.search(r'(\d+)\s+tests?\s+failed', clean, re.IGNORECASE)
        if m:
            return (int(m.group(1)), 0)

    # Maven: BUILD FAILURE with Tests run summary
    if re.search(r'BUILD FAILURE', clean, re.IGNORECASE):
        tr = re.search(r'Tests run:\s*(\d+),\s*Failures:\s*(\d+)', clean)
        if tr:
            total = int(tr.group(1))
            failed = int(tr.group(2))
            em = re.search(r'Errors:\s*(\d+)', clean)
            if em:
                failed += int(em.group(1))
            return (failed, total - failed)
        # BUILD FAILURE with no test counts — compilation/build failure
        return (1, 0)

    # Maven: BUILD SUCCESS with Tests run summary
    if re.search(r'BUILD SUCCESS', clean, re.IGNORECASE):
        tr = re.search(r'Tests run:\s*(\d+),\s*Failures:\s*(\d+)', clean)
        if tr:
            total = int(tr.group(1))
            failed = int(tr.group(2))
            em = re.search(r'Errors:\s*(\d+)', clean)
            if em:
                failed += int(em.group(1))
            return (failed, total - failed)

    

    
    if '_elixir_failures_' in results or '_elixir_passed_' in results:
        failed = int(results.get('_elixir_failures_', '0'))
        passed = int(results.get('_elixir_passed_', '0'))
        if failed > 0 or passed > 0:
            return (failed, passed)

    
    karma_failed = 0
    karma_passed = 0
    for k, v in results.items():
        if '_karma_summary_failed_' in k or '_karma_total_failed_' in k:
            if v.isdigit():
                karma_failed = int(v)
            else:
                m = re.search(r'(\d+)', k)
                if m:
                    karma_failed = int(m.group(1))
        if '_karma_summary_passed_' in k or '_karma_total_passed_' in k:
            if v.isdigit():
                karma_passed = int(v)
            else:
                m = re.search(r'(\d+)', k)
                if m:
                    karma_passed = int(m.group(1))
    if karma_failed > 0 or karma_passed > 0:
        return (karma_failed, karma_passed)

    
    for k, v in results.items():
        if '_summary_failed_' in k or '_tests_failed_' in k:
            
            match = re.search(r'(\d+)', k)
            if match:
                failed = int(match.group(1))
                
                passed = 0
                for k2, v2 in results.items():
                    if '_summary_passed_' in k2 or '_tests_passed_' in k2:
                        match2 = re.search(r'(\d+)', k2)
                        if match2:
                            passed = int(match2.group(1))
                            break
                return (failed, passed)
            
            if v.isdigit():
                failed = int(v)
                
                passed = 0
                for k2, v2 in results.items():
                    if '_summary_passed_' in k2 or '_tests_passed_' in k2:
                        if v2.isdigit():
                            passed = int(v2)
                            break
                        match2 = re.search(r'(\d+)', k2)
                        if match2:
                            passed = int(match2.group(1))
                            break
                return (failed, passed)
            return (1, 0)

    
    for k, v in results.items():
        if '_progress_' in k and '_failed_' in k:
            match = re.search(r'(\d+)', k)
            if match:
                failed = int(match.group(1))
                passed = 0
                for k2, v2 in results.items():
                    if '_progress_' in k2 and '_passed_' in k2:
                        match2 = re.search(r'(\d+)', k2)
                        if match2:
                            passed = int(match2.group(1))
                            break
                return (failed, passed)

    
    failed = len([v for v in results.values() if v in ('FAILED', 'ERROR')])
    passed = len([v for v in results.values() if v == 'PASSED'])
    return (failed, passed)


def parse_yara_output(output: str) -> Dict[str, str]:
    """Parse yara test output."""
    results = {}
    clean_output = strip_ansi(output)

    for line in clean_output.split('\n'):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # --- PASS N --- / --- FAIL N ---
        match = re.match(r'^---\s+(PASS|FAIL)\s+(\d+)\s+---', line_stripped)
        if match:
            status = 'PASSED' if match.group(1) == 'PASS' else 'FAILED'
            test_num = match.group(2)
            results[f'test_{test_num}'] = status
            continue

    return results


def parse_yosys_output(output: str) -> Dict[str, str]:
    """Parse yosys test output."""
    results = {}
    clean_output = strip_ansi(output)

    # Phase 1: Parse "Test: name" + result, handling both inline and multi-line formats
    current_test_name = None
    for line in clean_output.split('\n'):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # "Test: name -> ok/ERROR!" (inline format)
        match = re.match(r'^Test:\s+(\S+)\s+->\s*(ok|ERROR!?)', line_stripped)
        if match:
            test_name = match.group(1)
            status_str = match.group(2)
            results[test_name] = 'PASSED' if status_str.startswith('ok') else 'FAILED'
            current_test_name = None
            continue

        # "Test: name .." (multiline format — test starts, result comes later)
        match = re.match(r'^Test:\s+(\S+)\s+\.\.', line_stripped)
        if match:
            current_test_name = match.group(1)
            continue

        # "-> ok" / "-> ERROR!" (multiline format — test result)
        match = re.match(r'^->\s*(ok|ERROR!?)\s*$', line_stripped)
        if match and current_test_name:
            status_str = match.group(1)
            results[current_test_name] = 'PASSED' if status_str.startswith('ok') else 'FAILED'
            current_test_name = None
            continue

    # Phase 2: If no Test: results, try SAT verify / assertion format with context
    if not results:
        prev_command_file = None
        for line in clean_output.split('\n'):
            line_stripped = line.strip()

            # Track yosys commands to get test file name
            cmd_match = re.search(r'yosys\s+.*?(\w[\w.-]+\.(?:sv|v|ys))\s*$', line_stripped)
            if not cmd_match:
                cmd_match = re.search(r'yosys\s+-s\s+(\S+)', line_stripped)
            if not cmd_match:
                cmd_match = re.search(r'yosys\s+-ql\s+(\S+)', line_stripped)
            if cmd_match:
                fname = cmd_match.group(1)
                prev_command_file = re.sub(r'\.(sv|v|ys|log)$', '', fname)

            # ERROR: Called with -verify and proof did fail!
            if re.search(r'ERROR:\s+Called with -verify and proof did fail', line_stripped):
                if prev_command_file:
                    results[prev_command_file] = 'FAILED'
                else:
                    results['yosys_verify'] = 'FAILED'
                continue

            # ERROR: Assertion failed: message
            match = re.match(r'ERROR:\s+Assertion failed:\s+(.+)', line_stripped)
            if match:
                msg = match.group(1)[:80]
                if prev_command_file:
                    results[prev_command_file] = 'FAILED'
                else:
                    results[f'assertion: {msg}'] = 'FAILED'
                continue

    return results


def detect_test_framework(output: str) -> str:
    """Detect test framework."""
    if not output:
        return 'unknown'

    clean_output = strip_ansi(output)

    

    # YARA test-rules: --- PASS N --- / --- FAIL N --- (numeric IDs, no colon)
    # Must be before go_test, which uses --- PASS: Name (with colon)
    if re.search(r'^---\s+(?:PASS|FAIL)\s+\d+\s+---', clean_output, re.MULTILINE):
        return 'yara'

    # YosysHQ yosys: "Test: name ->ok" or "Test: name -> ERROR!" or multi-line "Test: name .." + "-> ok"
    if re.search(r'^Test:\s+\S+\s+->\s*(?:ok|ERROR!?)', clean_output, re.MULTILINE):
        return 'yosys'
    if re.search(r'^Test:\s+\S+\s+\.\.', clean_output, re.MULTILINE) and re.search(r'^->\s*(?:ok|ERROR!?)\s*$', clean_output, re.MULTILINE):
        return 'yosys'

    
    if 'Ginkgo ran' in clean_output:
        return 'ginkgo'
    if re.search(r'FAIL!\s*--\s*\d+\s+Passed.*\d+\s+Failed', clean_output, re.IGNORECASE):
        return 'ginkgo'

    
    if re.search(r'^--- (?:PASS|FAIL|SKIP):', clean_output, re.MULTILINE):
        return 'go_test'
    if re.search(r'^=== RUN\s+', clean_output, re.MULTILINE):
        return 'go_test'
    if re.search(r'^FAIL\s+[\w/\.\-]+\s+\[', clean_output, re.MULTILINE):
        return 'go_test'
    if re.search(r'^FAIL\s+github\.com/', clean_output, re.MULTILINE):
        return 'go_test'
    if re.search(r'^FAIL\s+[\w/\.\-]+\s*$', clean_output, re.MULTILINE):
        if re.search(r'go\s+test', clean_output, re.IGNORECASE) or '.go:' in clean_output:
            return 'go_test'
    if re.search(r'DONE\s+\d+\s+tests?\s*,\s*\d+\s+failures?', clean_output, re.IGNORECASE):
        return 'go_test'
    if re.search(r'^=== (?:Failed|FAIL:)', clean_output, re.MULTILINE):
        return 'go_test'
    # Go test: ok/PASS/FAIL package summary line (e.g., "ok  github.com/user/pkg  0.123s")
    if re.search(r'^(?:ok|PASS|FAIL)\s+[\w/\.\-]+(?:\s+\d|\s+\[)', clean_output, re.MULTILINE):
        return 'go_test'
    # Go test: FAIL with tab (FAIL\tpkgname)
    if re.search(r'^FAIL\t[\w/\.\-]+', clean_output, re.MULTILINE):
        return 'go_test'
    # Go test: OOPS/OK summary (e.g., "OOPS: 21 passed, 1 FAILED" or "OK: 22 passed")
    if re.search(r'^(?:OOPS|OK):\s*\d+\s+passed', clean_output, re.MULTILINE):
        return 'go_test'

    # Rust cargo test
    if re.search(r'^test\s+\S+\s+\.\.\.\s+(?:ok|FAILED|ignored)', clean_output, re.MULTILINE):
        return 'cargo'
    if re.search(r'running \d+ tests?', clean_output) and re.search(r'test result:', clean_output):
        return 'cargo'

    
    
    if re.search(r'\[gw\d+\]\s*(?:PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)', clean_output):
        return 'pytest'
    if re.search(r'\w+\.py::\w+.*(?:PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)', clean_output):
        return 'pytest'
    if re.search(r'^(?:PASSED|FAILED|ERROR)\s+\S+\.py::\w+', clean_output, re.MULTILINE):
        return 'pytest'
    if re.search(r'^test_\w+\s+(?:PASSED|FAILED|ERROR|SKIPPED)', clean_output, re.MULTILINE):
        return 'pytest'
    if re.search(r'\w+\.py\s+[.FxXsSE]+\s+\[', clean_output):
        return 'pytest'
    if re.search(r'=+\s*\d+\s+(?:failed|passed|error)', clean_output):
        return 'pytest_summary'

    
    if re.search(r'stestr\s+', clean_output, re.IGNORECASE) or 'stestr:' in clean_output.lower():
        return 'unittest'  
    
    if re.search(r'azdev\s+test', clean_output, re.IGNORECASE):
        return 'unittest'
    
    if re.search(r'iptest\s+', clean_output, re.IGNORECASE):
        return 'unittest'

    # Python: Cython/sympy runtests.py — "Ran N tests in Xs" + "OK" + "ALL DONE"
    if re.search(r'ALL DONE', clean_output) and re.search(r'Ran\s+\d+\s+tests?\s+in\s+[\d.]+s', clean_output, re.IGNORECASE):
        return 'sympy_runtests'
    if re.search(r'Running tests against Cython', clean_output, re.IGNORECASE):
        return 'sympy_runtests'
    if re.search(r'bin/test\b', clean_output) and re.search(r'Ran\s+\d+\s+tests?\s+in\s+[\d.]+s', clean_output, re.IGNORECASE):
        return 'sympy_runtests'

    # Python: nosetests (Ran X tests in Ys + OK/FAILED)
    if re.search(r'Ran\s+\d+\s+tests?\s+in\s+[\d.]+s', clean_output, re.IGNORECASE):
        if re.search(r'\nOK\b', clean_output) or re.search(r'\nFAILED', clean_output):
            return 'nosetests'
        return 'django_unittest'

    # Python: unittest / Django test
    if re.search(r'^\S+\s+\.\.\.\s+(?:ok|FAIL|ERROR|skip)', clean_output, re.MULTILINE):
        return 'unittest'

    
    

    # JerryScript
    if re.search(r'\d+\s+PASS,\s*\d+\s+FAIL', clean_output):
        return 'jerryscript'
    if re.search(r'\[summary\].*?PASS.*?FAIL', clean_output, re.IGNORECASE):
        return 'jerryscript'
    if re.search(r'PASS:\s*\d+\s+FAIL:\s*\d+', clean_output):
        return 'jerryscript'

    # SerenityOS: JS test runner (FAIL/PASS path.js + Test Suites:) — MUST be before jest
    if re.search(r'^\s*FAIL\s+\S+\.js\s*\(\d+ms\)', clean_output, re.MULTILINE) and re.search(r'Test Suites:', clean_output):
        return 'serenityos_js'

    
    if re.search(r'Test Suites:', clean_output):
        return 'jest'
    if re.search(r'Snapshots:', clean_output):
        return 'jest'
    if re.search(r'^(?:PASS|FAIL)\s+\S+\.(?:js|ts|jsx|tsx|cjs|mjs)', clean_output, re.MULTILINE):
        return 'jest'
    if re.search(r'jest-circus|jest-runner|jest-mock', clean_output):
        return 'jest'

    
    if re.search(r'RUN\s+v\d+\.\d+', clean_output):
        return 'vitest'
    if re.search(r'FAIL\s+test/\S+\.(?:ts|js|tsx|jsx)', clean_output):
        return 'vitest'
    if re.search(r'Test Files\s+\d+\s+(?:passed|failed)', clean_output, re.IGNORECASE):
        return 'vitest'
    if re.search(r'Snapshots\s+\d+\s+(?:passed|failed|updated)', clean_output, re.IGNORECASE):
        return 'vitest'

    
    if re.search(r'\d+\s+tests?\s+(?:passed|failed)', clean_output, re.IGNORECASE):
        if '✔' in clean_output or '✓' in clean_output:
            return 'ava'

    
    if re.search(r' karma\s', clean_output, re.IGNORECASE):
        return 'karma'
    if re.search(r'SUMMARY:', clean_output) and re.search(r'[✔✖]', clean_output):
        return 'karma'
    if re.search(r'Starting browser', clean_output) and re.search(r'[✔✖]', clean_output):
        return 'karma'
    if re.search(r'(?:Chrome|Firefox|Safari|Edge)\s+\d+', clean_output) and re.search(r'[✔✖]', clean_output):
        return 'karma'
    # Karma progress format: Executed N of M (X FAILED) / Executed N of M SUCCESS
    if re.search(r'Executed\s+\d+\s+of\s+\d+\s+\(\d+\s+FAILED\)', clean_output):
        return 'karma'
    if re.search(r'Executed\s+\d+\s+of\s+\d+\s+SUCCESS', clean_output):
        return 'karma'
    if re.search(r'(?:Chrome|Firefox) Headless.*Executed\s+\d+', clean_output):
        return 'karma'

    # JavaScript/TypeScript: Mocha
    if re.search(r'\d+\s+passing', clean_output, re.IGNORECASE):
        return 'mocha'
    if re.search(r'\d+\s+failing', clean_output, re.IGNORECASE):
        if re.search(r'^\s*\d+\)', clean_output, re.MULTILINE):
            return 'mocha'

    # TAP
    if re.search(r'^(?:ok|not ok)\s+\d+', clean_output, re.MULTILINE):
        return 'tap'
    if 'TAP version' in clean_output:
        return 'tap'

    # PHP: PHPUnit
    if re.search(r'FAILURES!', clean_output):
        return 'phpunit'
    if re.search(r'ERRORS!', clean_output) and re.search(r'Tests:\s*\d+,\s*Assertions:', clean_output):
        return 'phpunit'
    if re.search(r'OK\s*\(\d+\s+tests', clean_output):
        return 'phpunit'
    
    if re.search(r'^\w[\w\\]+\s+\(.+\)$', clean_output, re.MULTILINE) and re.search(r'[✔✘↩]', clean_output):
        return 'phpunit'

    # .NET: NUnit
    if re.search(r'Total tests:\s*\d+', clean_output, re.IGNORECASE):
        return 'nunit'
    if re.search(r'Test Run\s+(?:Passed|Failed)', clean_output, re.IGNORECASE):
        return 'nunit'

    
    if re.search(r'Failed!\s+-\s+Failed:\s*\d+.*Passed:\s*\d+.*Total:\s*\d+', clean_output):
        return 'xunit'
    if re.search(r'\[xUnit\.net\s+[\d:.]+\]', clean_output):
        return 'xunit'
    if re.search(r'Test run for.*\.dll', clean_output):
        return 'xunit'

    # Ruby: test/unit (Loaded suite is a strong indicator)
    if re.search(r'Loaded suite', clean_output):
        return 'ruby_unit'

    # Ruby: RSpec — check BEFORE Elixir since both have "Finished in X seconds"
    # RSpec uses "N examples, M failures" (not "N tests") and "Failed examples:" section
    if re.search(r'\d+\s+examples?', clean_output) and re.search(r'\d+\s+failures?', clean_output):
        return 'rspec'
    if re.search(r'Failed examples:', clean_output) and re.search(r'rspec\s+\S+', clean_output, re.IGNORECASE):
        return 'rspec'
    if re.search(r'\(FAILED\s*-\s*\d+\)', clean_output) and re.search(r'Finished in\s+[\d.]+\s+seconds?', clean_output, re.IGNORECASE):
        return 'rspec'

    # Elixir mix test
    if re.search(r'^\.\.\.(?:ok|error|excluded)', clean_output, re.MULTILINE):
        return 'elixir'
    if re.search(r'Finished in\s+[\d.]+\s+seconds', clean_output, re.IGNORECASE):
        # Only match Elixir if RSpec patterns are absent
        # Elixir has "N tests, M failures" (NOT "N examples")
        if not re.search(r'\d+\s+examples?', clean_output):
            return 'elixir'
    if re.search(r'Randomized with seed', clean_output):
        return 'elixir'
    if re.search(r'\d+\s+tests?\s*,\s*\d+\s+failures?', clean_output, re.IGNORECASE):
        if not re.search(r'Ran\s+\d+\s+tests?', clean_output, re.IGNORECASE) and 'DONE' not in clean_output:
            return 'elixir'
    if 'ex_unit' in clean_output.lower():
        return 'elixir'

    # Ruby: test/unit (Loaded suite + N tests, M assertions format)
    if re.search(r'Loaded suite', clean_output) and re.search(r'\d+\s+tests?\s*,\s*\d+\s+assertions?', clean_output):
        return 'ruby_unit'

    # Ruby: minitest — distinguish from RSpec and ruby_unit
    # "skips" is a minitest-specific indicator (ruby_unit doesn't have skips)
    if re.search(r'\d+\s+skips', clean_output) and re.search(r'\d+\s+assertions', clean_output):
        return 'minitest'
    # "N runs, M assertions" → minitest (not RSpec "examples", not ruby_unit "tests")
    if re.search(r'^\d+\s+runs', clean_output, re.MULTILINE) and re.search(r'\d+\s+assertions', clean_output):
        return 'minitest'
    # "N tests, M assertions, X failures, Y errors" WITH "Failure:" section → minitest
    # (ruby_unit uses different failure format like "test_name: F")
    if re.search(r'\d+\s+tests?\s*,\s*\d+\s+assertions?\s*,\s*\d+\s+failures?\s*,\s*\d+\s+errors?', clean_output):
        if re.search(r'^\s*\d*\)?\s*Failure:', clean_output, re.MULTILINE):
            return 'minitest'
        return 'ruby_unit'

    # Dart / Flutter test
    if re.search(r'\d+:\d+\s+[+-]\d+:', clean_output):
        return 'dart'
    if 'Some tests failed.' in clean_output:
        return 'dart'

    # Java: Maven (Tests run: X, Failures: Y)
    if re.search(r'Tests run:\s*\d+,\s*Failures:', clean_output):
        return 'maven'
    # Maven: BUILD FAILURE without Surefire output
    if re.search(r'\[(?:INFO|ERROR)\]\s*(?:-+\s*)?BUILD FAILURE', clean_output):
        return 'maven'

    
    if re.search(r'> Task :test\s', clean_output):
        return 'gradle_custom'
    if re.search(r'Gradle\s+test\s+run', clean_output, re.IGNORECASE):
        return 'gradle_custom'

    # Java: Ant ([junit] PASS/FAIL)
    if re.search(r'\[junit\]\s+\[(?:PASS|FAIL|ERR)\]', clean_output):
        return 'ant'

    # AssemblyScript
    if re.search(r'FAILURE:\s*\d+\s+.*tests?\s+had\s+failures?', clean_output, re.IGNORECASE):
        return 'assemblyscript'
    if re.search(r'SUCCESS\s+\([\d.]+\s*ms\)', clean_output):
        return 'assemblyscript'

    # Web Test Runner
    if re.search(r'(?:Chromium|Firefox|Webkit|Safari|Chrome):\s*\|.*\d+\s+passed', clean_output, re.IGNORECASE):
        return 'web_test_runner'
    if re.search(r'Finished running tests', clean_output) and re.search(r'\d+\s+passed,\s*\d+\s+failed', clean_output):
        return 'web_test_runner'

    # Go testify
    if re.search(r'Tests:\s*\d+,\s*Assertions:\s*\d+,\s*Failures?:\s*\d+', clean_output):
        return 'go_testify'

    # SerenityOS: custom test runner (Running test 'name'/Completed/Failed test 'name')
    if re.search(r"^Running test '", clean_output, re.MULTILINE) and re.search(r"(?:Completed|Failed) test '", clean_output, re.MULTILINE):
        return 'serenityos_test'
    # SerenityOS: headless-browser layout test (N/M: Layout/input/... + Pass: X, Fail: Y)
    if re.search(r'^\d+/\d+:\s+(?:Layout|Text)/input/', clean_output, re.MULTILINE) and re.search(r'Pass:\s*\d+,\s*Fail:\s*\d+', clean_output):
        return 'headless_browser'
    # SerenityOS: headless-browser "Test PASSED/FAILED: name output"
    if re.search(r'^Test (?:PASSED|FAILED):', clean_output, re.MULTILINE):
        return 'headless_browser'

    # TSLint
    if re.search(r'Testing Lint Rules:', clean_output):
        return 'tslint'
    if re.search(r'test/rules/[^:]+:\s*(?:Passed|Failed|Skipped)', clean_output):
        return 'tslint'

    # fluent-bit: Test name... [ OK ] / [ FAILED ] (must be before cpp_test which also matches [FAILED])
    if re.search(r'Test\s+\S+\s*\.\.\.\s*\[\s*(?:OK|FAILED|PASS)\s*\]', clean_output, re.MULTILINE):
        return 'fluent_bit'

    # C++: Google Test / Catch2
    if re.search(r'\[\s*={10,}\s*\].*Running\s+\d+\s+tests', clean_output, re.IGNORECASE):
        return 'cpp_test'
    if re.search(r'\[\s*PASSED\s*\]\s*\d+\s+tests?', clean_output, re.IGNORECASE):
        return 'cpp_test'
    if re.search(r'\d+\s+FAILED\s+TEST', clean_output, re.IGNORECASE):
        return 'cpp_test'
    if re.search(r'\[\s*FAILED\s*\]', clean_output) and re.search(r'\[\s*OK\s*\]', clean_output):
        return 'cpp_test'

    # C/C++: CMake ctest
    if re.search(r'ctest\s', clean_output, re.IGNORECASE) and re.search(r'\d+\s+tests?\s+(?:passed|failed|not passed)', clean_output, re.IGNORECASE):
        return 'ctest'

    # C: Valkey/custom unit test format: [ok] - file:test / [fail] - file:test / N tests, M passed, K failed
    if re.search(r'\[(?:ok|fail)\]\s*-\s*\S+:', clean_output) and re.search(r'\d+\s+tests?\s*,\s*\d+\s+passed', clean_output, re.IGNORECASE):
        return 'ctest'

    # C: Check framework (e.g., fluent-bit)
    if re.search(r'Count of (?:all |)unit tests:', clean_output, re.IGNORECASE):
        return 'check'
    if re.search(r'\d+\s+unit tests:\s*\d+\s+passed,\s*\d+\s+failed', clean_output, re.IGNORECASE):
        return 'check'
    if re.search(r'SUCCESS:\s*All (?:conditions|unit tests) have passed', clean_output, re.IGNORECASE):
        if re.search(r'\bCheck\b', clean_output):
            return 'check'
    if re.search(r'Running suite\S*:\s*\d+', clean_output, re.IGNORECASE) and re.search(r'\bCheck\b', clean_output):
        return 'check'
    if re.search(r'^\d+/\d+\s+Test\s+#\d+:', clean_output, re.MULTILINE):
        return 'ctest'
    if re.search(r'\d+%\s+tests\s+passed', clean_output, re.IGNORECASE) and re.search(r'\d+\s+tests?\s+did\s+not\s+pass', clean_output, re.IGNORECASE):
        return 'ctest'

    # C/C++: cppinsights ([PASSED]/[FAILED] file.cpp + Tests passed: N/M)
    if re.search(r'\[(?:PASSED|FAILED)\]\s+\S+\.\w+\s+-', clean_output) and re.search(r'Tests passed:\s*\d+/\d+', clean_output):
        return 'cppinsights'

    # Shell: BATS (Bash Automated Testing System)
    if re.search(r'^(?:ok|not ok)\s+\d+\s+', clean_output, re.MULTILINE) and 'TAP version' not in clean_output:
        return 'bats'

    # AWS s2n-tls: PASSED N tests / FAILED test N
    if re.search(r'PASSED\s+\d+\s+tests?', clean_output) and re.search(r'FAILED\s+test\s+\d+', clean_output):
        return 's2n'
    if re.search(r'Running\s+\S+_test\.c\s+\.\.\.\s+PASSED\s+\d+\s+tests?', clean_output):
        return 's2n'

    # JUnit standalone (not Maven): FAILURES!!! Tests run: N, Failures: M (without Maven markers)
    if re.search(r'FAILURES!!!\s*Tests run:\s*\d+,\s*Failures:', clean_output):
        return 'junit_summary'
    # tlaplus: JUnit format run standalone
    if re.search(r'Tests run:\s*\d+,\s*Failures:\s*\d+$', clean_output, re.MULTILINE):
        return 'junit_summary'

    # Python unittest: Ran N tests in Xs + FAILED/OK
    if re.search(r'Ran\s+\d+\s+tests?\s+in\s+[\d.]+s', clean_output, re.IGNORECASE):
        if re.search(r'FAILED\s*\(\s*(errors|failures)=\d+', clean_output):
            return 'python_unittest'
        if re.search(r'^OK\s*$', clean_output, re.MULTILINE):
            return 'python_unittest'

    
    if re.search(r'\d+\s+(?:test|tests)\s+(?:passed|failed)', clean_output, re.IGNORECASE):
        return 'summary_only'

    
    if '✓' in clean_output or '✕' in clean_output or '✗' in clean_output:
        return 'jest'
    if '✔' in clean_output or '✖' in clean_output:
        return 'mocha'
    if '√' in clean_output or '×' in clean_output:
        return 'jest'

    return 'unknown'



LANGUAGE_PARSERS = {
    'Python': ['pytest', 'unittest', 'nosetests', 'django_unittest', 'pytest_summary', 'sympy_runtests'],
    'Go': ['go_test', 'ginkgo', 'go_testify'],
    'JavaScript': ['jest', 'mocha', 'karma', 'ava', 'tap'],
    'TypeScript': ['jest', 'mocha', 'vitest', 'karma'],
    'PHP': ['phpunit'],
    'Rust': ['cargo'],
    'C': ['ctest', 'cpp_test', 'check', 'serenityos_test', 'serenityos_js', 'headless_browser', 'yara', 'yosys', 's2n', 'fluent_bit', 'redis', 'jq', 'micropython', 'doctest'],
    'C++': ['cpp_test', 'ctest', 'check', 'serenityos_test', 'serenityos_js', 'headless_browser', 'yosys', 'fluent_bit', 'doctest'],
    'Ruby': ['rspec', 'minitest', 'ruby_unit'],
    'Java': ['maven', 'maven_individual', 'gradle_custom', 'ant', 'junit_summary'],
}


LOG_PARSER_MAP = {
    'parse_log_pytest': 'pytest',
    'parse_log_pytest_v2': 'unittest',
    'parse_log_django': 'django_unittest',
    'parse_log_gotest': 'go_test',
    'parse_log_jest': 'jest',
    'parse_log_jest_ts': 'jest',
    'parse_log_vitest': 'vitest',
    'parse_log_vitest_ts': 'vitest',
    'parse_log_mocha_ts': 'mocha',
    'parse_log_phpunit': 'phpunit',
    'parse_log_cargo': 'cargo',
    'parse_log_rspec_transformed_json': 'rspec',
    'parse_log_minitest': 'minitest',
    'parse_log_maven': 'maven',
    'parse_log_maven_individual': 'maven_individual',
    'parse_log_gradle_custom': 'gradle_custom',
    'parse_log_googletest': 'cpp_test',
    'parse_log_ctest': 'ctest',
    'parse_log_catch2': 'cpp_test',
    'parse_log_karma': 'karma',
    'parse_log_tap': 'tap',
    'parse_log_tap_ts': 'tap',
    'parse_log_micropython_test': 'micropython',
    'parse_log_sympy': 'sympy_runtests',
    'parse_log_ruby_unit': 'ruby_unit',
    'parse_log_ant': 'ant',
    'parse_log_boost_test': 'cpp_test',
    'parse_log_doctest': 'doctest',
    'parse_log_redis': 'redis',
    'parse_log_jq': 'jq',
    'parse_log_cucumber': 'rspec',
    'parse_log_check': 'check',
    'parse_log_assemblyscript': 'assemblyscript',
    'parse_log_cppinsights': 'cppinsights',
    'parse_log_gradle': 'gradle_custom',
}


def parse_test_output(output: str, language: str = None, test_framework: str = None, log_parser: str = None) -> Dict[str, str]:
    """Parse test test output."""
    if not output:
        return {}

    # Truncate to last 200KB — test names/statuses are interspersed throughout
    # the output, but the leading hundreds of KB are usually build/compile noise.
    # Without truncation, regex matching on 4MB logs causes catastrophic backtracking.
    _MAX_PARSE_LEN = 500_000
    if len(output) > _MAX_PARSE_LEN:
        output = output[-_MAX_PARSE_LEN:]

    
    framework_parsers = {
        'pytest': parse_pytest_output,
        'go_test': parse_go_test_output,
        'jest': parse_jest_output,
        'mocha': parse_mocha_output,
        'phpunit': parse_phpunit_output,
        'unittest': parse_unittest_output,
        'django_unittest': parse_unittest_output,  
        'nosetests': parse_nosetests_output,
        'tap': parse_tap_output,
        'rspec': parse_rspec_output,
        'minitest': parse_minitest_output,
        'elixir': parse_elixir_output,
        'dart': parse_dart_output,
        'jerryscript': parse_jerryscript_output,
        'maven': parse_maven_output,
        'maven_individual': parse_maven_individual_output,
        'ava': parse_ava_output,
        'vitest': parse_vitest_output,
        'ginkgo': parse_ginkgo_output,
        'cpp_test': parse_cpp_test_output,
        'xunit': parse_xunit_output,
        'tslint': parse_tslint_output,
        'web_test_runner': parse_web_test_runner_output,
        'go_testify': parse_go_testify_output,
        'sympy_runtests': parse_nosetests_output,  # Cython/sympy runtests: "Ran N tests OK" → nosetests format
        'ruby_unit': parse_ruby_test_unit_output,   # Ruby test/unit: N tests, M assertions format
        
        'cargo': parse_unittest_output,        
        'karma': parse_karma_output,           
        'ant': parse_ant_junit_output,         
        'nunit': parse_xunit_output,           
        'gradle_custom': parse_gradle_output,        # Gradle Test Run Results / per-test format
        'cppinsights': parse_cppinsights_output,    # cppinsights [PASSED]/[FAILED] format
        'bats': parse_tap_output,              
        'ctest': parse_ctest_output,            
        'check': parse_check_output,            # C Check framework (e.g., fluent-bit)
        'micropython': parse_micropython_output,  # MicroPython: pass/FAIL/skip test_name
        'assemblyscript': parse_assemblyscript_output,  # AssemblyScript: - test_name SUCCESS/FAILURE
        'serenityos_test': parse_serenityos_test_output,  # SerenityOS test runner: Running/Completed/Failed test
        'serenityos_js': parse_serenityos_js_test_output,  # SerenityOS JS test runner: FAIL/PASS path.js
        'headless_browser': parse_headless_browser_test_output,  # SerenityOS headless-browser: N/M: path + Pass/Fail summary
        'yara': parse_yara_output,                            # YARA test-rules: --- PASS N --- / --- FAIL N ---
        'yosys': parse_yosys_output,                          # YosysHQ yosys: Test: name ->ok/ERROR!
        'junit_summary': parse_junit_summary_output,          # JUnit/TLC: Tests run: N, Failures: M
        'python_unittest': parse_python_unittest_output,      # Python unittest: Ran N tests in Xs, FAILED (errors=N)
        's2n': parse_s2n_output,                              # AWS s2n-tls: PASSED N tests / FAILED test N
        'fluent_bit': parse_fluentbit_output,                 # fluent-bit: Test name... [ OK ] / [ FAILED ]
        'redis': parse_redis_output,                         # Redis: [ok/err/skip]: test_name
        'jq': parse_jq_output,                               # jq: PASS/FAIL: test_name
        'doctest': parse_doctest_output,                     # Doctest XML: <TestCase><SubCase> format
    }

    
    
    
    
    if log_parser and log_parser in LOG_PARSER_MAP:
        fw = LOG_PARSER_MAP[log_parser]
        if fw in framework_parsers:
            try:
                results = framework_parsers[fw](output)
                if results:
                    logger.debug(f"log_parser={log_parser} -> framework={fw}, parsed {len(results)} tests")
                    return results
                
                detected = detect_test_framework(output)
                if detected != fw and detected in framework_parsers:
                    try:
                        fallback_results = framework_parsers[detected](output)
                        if fallback_results:
                            logger.debug(f"log_parser={log_parser} -> {fw} returned no results; fallback={detected} parsed {len(fallback_results)} tests")
                            return fallback_results
                    except Exception:
                        pass
                logger.debug(f"log_parser={log_parser} -> framework={fw}, parsed 0 tests including fallback")
                return results
            except Exception as e:
                logger.debug(f"log_parser={log_parser} -> framework={fw} parser failed: {e}")

    
    if test_framework and test_framework in framework_parsers:
        try:
            results = framework_parsers[test_framework](output)
            results = clean_test_results(results)
            if results:
                logger.debug(f"explicit framework={test_framework}, parsed {len(results)} tests")
                return results
            
            detected = detect_test_framework(output)
            if detected != test_framework and detected in framework_parsers:
                try:
                    fallback_results = framework_parsers[detected](output)
                    fallback_results = clean_test_results(fallback_results)
                    if fallback_results:
                        logger.debug(f"explicit framework={test_framework} returned no results; fallback={detected} parsed {len(fallback_results)} tests")
                        return fallback_results
                except Exception:
                    pass
            logger.debug(f"explicit framework={test_framework}, parsed 0 tests including fallback")
            return results
        except Exception as e:
            logger.debug(f"explicit framework={test_framework} parser failed: {e}")

    
    framework = detect_test_framework(output)
    logger.debug(f"detected test framework: {framework}")

    
    allowed_frameworks = LANGUAGE_PARSERS.get(language) if language else None

    if allowed_frameworks:
        if framework in allowed_frameworks and framework in framework_parsers:
            try:
                results = framework_parsers[framework](output)
                results = clean_test_results(results)
                logger.debug(f"language={language}, framework={framework}, parsed {len(results)} tests")
                return results
            except Exception as e:
                logger.debug(f"{framework} parser failed: {e}")

        
        best_results = {}
        best_fw = None
        for fw_name in allowed_frameworks:
            parser = framework_parsers.get(fw_name)
            if not parser:
                continue
            try:
                results = parser(output)
                results = clean_test_results(results)
                if len(results) > len(best_results):
                    best_results = results
                    best_fw = fw_name
            except:
                pass
        if best_results:
            logger.debug(f"language={language}, framework={framework} out of scope; best={best_fw}, parsed {len(best_results)} tests")
            return best_results

        
        if framework == 'pytest_summary':
            error_results = {}
            for m in re.finditer(r'^(ERROR)\s+(\S+)', output, re.MULTILINE):
                error_results[m.group(2)] = 'ERROR'
            for m in re.finditer(r'^(FAILED)\s+(\S+)', output, re.MULTILINE):
                error_results[m.group(2)] = 'FAILED'
            for m in re.finditer(r'^(PASSED)\s+(\S+)', output, re.MULTILINE):
                error_results[m.group(2)] = 'PASSED'
            if error_results:
                return error_results
            match = re.search(r'(\d+)\s+failed', output)
            errors_match = re.search(r'(\d+)\s+errors?', output)
            failed_total = 0
            if match:
                failed_total += int(match.group(1))
            if errors_match:
                failed_total += int(errors_match.group(1))
            if failed_total > 0:
                return {'_summary_failed_': 'FAILED'}

        if framework == 'summary_only':
            match = re.search(r'(\d+)\s+(?:test|tests)\s+failed', output, re.IGNORECASE)
            if match and int(match.group(1)) > 0:
                return {'_summary_failed_': 'FAILED'}

        return {}

    
    if framework in framework_parsers:
        try:
            results = framework_parsers[framework](output)
            results = clean_test_results(results)
            logger.debug(f"framework={framework}, parsed {len(results)} tests")
            return results
        except Exception as e:
            logger.debug(f"{framework} parser failed: {e}")

    
    if framework == 'pytest_summary':
        error_results = {}
        for m in re.finditer(r'^(ERROR)\s+(\S+)', output, re.MULTILINE):
            error_results[m.group(2)] = 'ERROR'
        for m in re.finditer(r'^(FAILED)\s+(\S+)', output, re.MULTILINE):
            error_results[m.group(2)] = 'FAILED'
        for m in re.finditer(r'^(PASSED)\s+(\S+)', output, re.MULTILINE):
            error_results[m.group(2)] = 'PASSED'
        if error_results:
            return error_results
        match = re.search(r'(\d+)\s+failed', output)
        errors_match = re.search(r'(\d+)\s+errors?', output)
        failed_total = 0
        if match:
            failed_total += int(match.group(1))
        if errors_match:
            failed_total += int(errors_match.group(1))
        if failed_total > 0:
            return {'_summary_failed_': 'FAILED'}

    if framework == 'summary_only':
        match = re.search(r'(\d+)\s+(?:test|tests)\s+failed', output, re.IGNORECASE)
        if match and int(match.group(1)) > 0:
            return {'_summary_failed_': 'FAILED'}

    
    if framework == 'unknown':
        best_results = {}
        for name, parser in framework_parsers.items():
            try:
                results = parser(output)
                results = clean_test_results(results)
                if len(results) > len(best_results):
                    best_results = results
            except:
                pass
        if best_results:
            logger.debug(f"unknown framework; best parser produced {len(best_results)} tests")
        return best_results

    return {}


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
def extract_test_case_name(full_name: str) -> str:
    if not full_name:
        return full_name
    if "(" in full_name:
        return full_name.split("(")[0].strip()
    if "::" in full_name:
        return full_name.split("::")[-1].split("[")[0]
    if "." in full_name:
        return full_name.split(".")[-1]
    return full_name


def match_test_name(test_id: str, status_map: Dict[str, str]) -> Optional[str]:
    """Match test name."""
    if not test_id or not status_map:
        return None

    name = extract_test_case_name(test_id)

    # Exact match
    if test_id in status_map:
        return test_id
    if name in status_map:
        return name

    # Case-insensitive exact match
    name_lower = name.lower()
    for key in status_map:
        if key.lower() == name_lower:
            return key

    # Strip parameterized suffix [param] and match
    name_no_param = re.sub(r'\[.*?\]', '', name)
    for key in status_map:
        key_no_param = re.sub(r'\[.*?\]', '', key)
        if name_no_param and key_no_param and name_no_param.lower() == key_no_param.lower():
            return key

    # Go: match TestName against pkg.TestName or pkg.TestName/SubTest
    for key in status_map:
        if '.' in key:
            key_short = key.rsplit('.', 1)[-1]
            if '/' in key_short:
                key_base = key_short.split('/')[0]
                if key_base.lower() == name_lower:
                    return key
            if key_short.lower() == name_lower:
                return key

    # Short form: Class::method
    test_id_parts = test_id.split("::")
    if len(test_id_parts) >= 2:
        short_id = "::".join(test_id_parts[-2:])
        if short_id in status_map:
            return short_id

    # File::Class::method → filename::rest
    if len(test_id_parts) >= 2:
        fname = test_id_parts[0].split("/")[-1]
        rest = "::".join(test_id_parts[1:])
        candidate = f"{fname}::{rest}"
        if candidate in status_map:
            return candidate
        for key in status_map:
            if key.endswith(f"::{rest}"):
                return key

    return None
