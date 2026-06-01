"""
FISH launch_test inspector — parallel to `fish.launch_inspect` for the
`launch_test` (launch_testing) entry point used by Isaac ROS Benchmark
and other pytest-style integration suites.

ros2 launch         ── package + launch.py    ──► launch.LaunchService(LaunchDescription)
launch_test         ── test_script.py         ──► launch_testing builds a TestRun, then
                                                   uses launch.LaunchService(LaunchDescription)

Same execution engine, same Node/ComposableNodeContainer machinery. We just
load the LaunchDescription differently: a launch_test script exposes it via
`generate_test_description()` (sometimes returning `(ld, context_dict)`).

Usage:
    python3 -m fish.launch_test_inspect <test_script_path> [arg:=val ...]

Output: same JSON shape as fish.launch_inspect — `launch_components.json`
with `__gpu_containers__` and `__gpu_nodes__` keys consumed by
`fish.launch_wrap.install_patches()`.

On any error, emits "{}" and exits 0 so the bash wrapper can continue.
"""
import importlib.util
import inspect
import json
import os
import sys
import traceback

from fish.launch_inspect import _patch_captures, inspect_launch_description


def _import_test_script(path: str):
    """Import a test script by absolute path under a stable module name."""
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(abs_path)
    spec = importlib.util.spec_from_file_location("fish_launch_test_module", abs_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build import spec for {abs_path}")
    module = importlib.util.module_from_spec(spec)
    # Allow the script to find sibling files via sys.path
    script_dir = os.path.dirname(abs_path)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec.loader.exec_module(module)
    return module


def _call_generate_test_description(module):
    """Invoke generate_test_description(), handling 0-arg and 1-arg signatures.

    The launch_testing API expects either:
      - `def generate_test_description():`                 (0 args)
      - `def generate_test_description(ready_fn):`         (1 arg, deprecated)
    Newer pytest-style tests may also use the @launch_testing.parametrize
    decorator; we don't try to handle that — fall back gracefully.
    """
    fn = getattr(module, "generate_test_description", None)
    if fn is None:
        raise RuntimeError(
            f"{module.__file__} has no generate_test_description()"
        )

    try:
        sig = inspect.signature(fn)
        nparams = len(
            [p for p in sig.parameters.values()
             if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                           inspect.Parameter.POSITIONAL_ONLY)
             and p.default is inspect.Parameter.empty]
        )
    except (TypeError, ValueError):
        nparams = 0

    if nparams == 0:
        return fn()
    # 1-arg legacy signature wants a ReadyToTest-like action
    try:
        from launch_testing.actions import ReadyToTest
        return fn(ReadyToTest())
    except Exception:
        return fn(None)


def inspect_launch_test(test_script: str, launch_args: list) -> dict:
    """Main inspect entry — returns the same JSON structure as inspect_launch."""
    _patch_captures()

    from launch import LaunchService

    module = _import_test_script(test_script)
    result = _call_generate_test_description(module)

    # Result can be a LaunchDescription, or a tuple (LaunchDescription, dict)
    # in newer launch_testing — extract the description.
    if isinstance(result, tuple):
        ld = result[0]
    else:
        ld = result

    service = LaunchService()
    context = service.context

    # Seed launch configurations from CLI arguments (key:=value form)
    for arg in launch_args:
        if ":=" in arg:
            k, _, v = arg.partition(":=")
            context.launch_configurations[k] = v

    return inspect_launch_description(ld, context)


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: python3 -m fish.launch_test_inspect <test_script> [arg:=val ...]",
            file=sys.stderr,
        )
        return 2

    test_script = sys.argv[1]
    launch_args = sys.argv[2:]

    # Keep stdout clean for JSON; route framework chatter to stderr.
    original_stdout = sys.stdout
    sys.stdout = sys.stderr

    try:
        result = inspect_launch_test(test_script, launch_args)
    except Exception as e:
        sys.stdout = original_stdout
        print(f"[FISH] launch_test_inspect error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print("{}")
        return 0

    sys.stdout = original_stdout
    json.dump(result, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
