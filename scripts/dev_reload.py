#!/usr/bin/env python3
"""Development now uses a fresh private Krita instance, never remote exec."""
import sys
from pathlib import Path
import subprocess

if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    args = sys.argv[1:] or ['--output', 'artifacts/dev', '--', 'python', '-m', 'unittest', '-v', 'tests.test_workflow']
    raise SystemExit(subprocess.call([sys.executable, str(root/'scripts/run_isolated_tests.py'), *args]))
