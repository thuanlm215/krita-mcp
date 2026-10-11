#!/usr/bin/env python3
"""Run a command against a disposable Krita/Xvfb; never install on the desktop.

python scripts/run_isolated_tests.py --output artifacts/implementation -- \
    python -m unittest -v tests.test_revision tests.test_workflow tests.test_advanced
"""
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def native_diagnostics(output):
    """A test assertion can pass despite a recoverable native assertion."""
    pattern = re.compile(r'SAFE ASSERT|\bASSERT(?:ION)?(?:\s*[:(]|\s+(?:FAILED|FAILURE))|'
                         r'assertion .*failed|Segmentation fault|SIGSEGV|SIGABRT|'
                         r'Fatal Python error|Traceback \(most recent call last\)', re.I)
    return [{'file': path.name, 'line': i, 'message': line}
            for name in ('krita.log', 'native-krita.log')
            if (path := output / name).exists()
            for i, line in enumerate(path.read_text(errors='replace').splitlines(), 1)
            if pattern.search(line)]


def group_alive(pgid):
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.getpgid(int(entry.name)) == pgid and entry.joinpath("stat").read_text().rsplit(
                    ")", 1)[1].split()[0] != "Z":
                return True
        except OSError:
            pass
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/implementation")
    parser.add_argument("--recovery-fixture", type=Path,
                        help="Seed two native startup autosaves from this KRA in the private test home")
    parser.add_argument('--no-fixture-hooks', action='store_true',
                        help='Test the normal installable plugin with private assertions absent')
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    command = command or [sys.executable, "-m", "unittest", "-v",
                          "tests.test_revision", "tests.test_workflow", "tests.test_advanced"]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix="krita-mcp-test-"))
    env = os.environ.copy()
    for key in ("KRITA_MCP_PORT", "KRITA_MCP_TOKEN", "KRITA_MCP_HOST", "KRITA_MCP_INFO_FILE", "WAYLAND_DISPLAY"):
        env.pop(key, None)
    for key, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                        ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "run"),
                        ("TMPDIR", "tmp"), ("KRITA_MCP_STATE_DIR", "state")):
        path = temp / folder
        path.mkdir(mode=0o700)
        env[key] = str(path)
    env.update(QT_QPA_PLATFORM="xcb", KRITA_MCP_TRACE="1", KRITA_MCP_TEST_ARTIFACTS=str(output))
    # Linux recovery scans QDir.homePath(), independently of XDG/TMPDIR.
    # Set the home of this private subprocess only; never scan desktop autosaves.
    test_home = temp / "home"
    test_home.mkdir(mode=0o700)
    env["HOME"] = str(test_home)
    if args.recovery_fixture:
        fixture = args.recovery_fixture.resolve()
        for name in (".krita-0-mcp-recovery-a-autosave.kra", "krita-0-mcp-recovery-b-autosave.kra"):
            shutil.copy2(fixture, test_home / name)
    resources = temp / "data/krita"
    resources.joinpath("pykrita").mkdir(parents=True)
    shutil.copytree(ROOT / "pykrita/krita_mcp", resources / "pykrita/krita_mcp",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "pykrita/krita_mcp.desktop", resources / "pykrita/krita_mcp.desktop")
    # This assertion plugin has no public MCP schema and is never installed
    # by install.sh/install.ps1. It accepts fixed scenarios, not Python text.
    if not args.no_fixture_hooks:
        shutil.copy2(ROOT / "tests/fixture_plugin.py", resources / "pykrita/krita_mcp/_test_fixture.py")
        with resources.joinpath("pykrita/krita_mcp/__init__.py").open('a') as init:
            init.write('\nfrom . import _test_fixture\n')
    temp.joinpath("config/kritarc").write_text(
        "[python]\nenable_krita_mcp=true\n[krita_mcp]\nautostart=true\n"
        "[General]\nOpenGLRenderer=none\nHideWelcomeScreen=true\n", encoding="utf-8")
    summary = {"command": command, "runtime": str(temp), "output": str(output)}
    krita = xvfb = None
    code = 1
    with output.joinpath("xvfb.log").open("w") as xlog, output.joinpath("krita.log").open("w") as klog:
        try:
            xvfb = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "1280x900x24",
                                     "-nolisten", "tcp"], stdout=subprocess.PIPE, stderr=xlog, text=True)
            env["DISPLAY"] = ":" + xvfb.stdout.readline().strip()
            krita = subprocess.Popen(["dbus-run-session", "--", "krita", "--nosplash",
                                      "--resource-location", str(resources)], env=env,
                                     stdout=klog, stderr=subprocess.STDOUT, start_new_session=True)
            summary["wrapper_pid"] = krita.pid
            deadline = time.monotonic() + 90
            while not temp.joinpath("state/krita_mcp_bridge.json").exists():
                if krita.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Isolated Krita bridge did not start; see krita.log")
                time.sleep(.2)
            started = time.monotonic()
            with output.joinpath("tests.log").open("w") as log:
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                try:
                    code = process.wait(timeout=900)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise
            summary.update(exit_code=code, elapsed_seconds=round(time.monotonic() - started, 3))
            print("Test command exit:", code, flush=True)
        finally:
            if krita is not None:
                summary["krita_exit_before_cleanup"] = krita.poll()
                # dbus-run-session is a wrapper. Stop its complete private
                # group and wait for the Krita child BEFORE stopping Xvfb.
                # Ask the private application's event loop to quit first.
                # No SIGKILL, including on failed tests; desktop is untouched.
                if group_alive(krita.pid):
                    try:
                        from mcp_server import BridgeClient
                        cleanup = BridgeClient()
                        cleanup_info = dict(json.loads(temp.joinpath("state/krita_mcp_bridge.json").read_text()),
                                            host="127.0.0.1")
                        # Even if the private bridge dies mid-cleanup, a retry
                        # must never rediscover and quit the desktop bridge.
                        cleanup._load_info = lambda force=False: cleanup_info
                        cleanup.call("_test_fixture", {"scenario": "shutdown"}, timeout=3)
                        summary["graceful_quit_requested"] = True
                    except Exception as exc:
                        summary["graceful_quit_error"] = type(exc).__name__
                    deadline = time.monotonic() + 8
                    while group_alive(krita.pid) and time.monotonic() < deadline:
                        time.sleep(.1)
                for sig in (signal.SIGTERM,):
                    if not group_alive(krita.pid):
                        break
                    try:
                        os.killpg(krita.pid, sig)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + 10
                    while group_alive(krita.pid) and time.monotonic() < deadline:
                        time.sleep(.1)
                    if not group_alive(krita.pid):
                        break
                summary["process_group_stopped_before_xvfb"] = not group_alive(krita.pid)
                summary["force_kill_used"] = False
                krita.wait(timeout=5)
            if xvfb is not None:
                xvfb.terminate()
                xvfb.wait(timeout=5)
            for trace in temp.joinpath("state").glob("*trace*"):
                shutil.copy2(trace, output / trace.name)
            for filename in ('krita.log','krita-sysinfo.log'):
                usage=temp/'data'/filename
                if usage.exists():shutil.copy2(usage,output/('native-'+filename))
            summary['native_diagnostics'] = native_diagnostics(output)
            summary['test_exit_code'] = code
            if (summary['native_diagnostics'] or summary.get('krita_exit_before_cleanup') is not None) and code == 0:
                code = 1
            summary['exit_code'] = code
            output.joinpath("summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
