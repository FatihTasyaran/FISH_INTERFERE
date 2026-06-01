"""
FISH launch_test wrapper — parallel to `fish.launch_wrap` for the
`launch_test` entry point.

Installs the same `Node.execute` / `ComposableNodeContainer.execute`
monkey-patches that `fish.launch_wrap` installs for `ros2 launch`, then
delegates to the real launch_testing CLI so every spawned ROS process
inherits the nsys prefix from t=0 (no kill+resurrect roundtrip needed).

Usage:
    python3 -m fish.launch_test_wrap <test_script_path> [args ...]

Invoked by `/opt/ros/humble/fish/bin/launch_test` after
`fish.launch_test_inspect` has populated `launch_components.json`.
"""
import sys


def main() -> int:
    # Install Node.execute + ComposableNodeContainer.execute monkey-patches
    # using the same logic the ros2 launch flow uses.
    from fish.launch_wrap import install_patches
    install_patches()

    # Hand off to the real launch_test CLI entry. launch_testing internally
    # constructs a launch.LaunchService — the same engine ros2 launch uses —
    # so the monkey-patches applied above take effect for every spawned
    # process exactly as they do under `ros2 launch`.
    try:
        from launch_testing.launch_test import main as lt_main
    except ImportError as e:
        print(f"[FISH launch_test_wrap] launch_testing import failed: {e}",
              file=sys.stderr)
        return 2

    # launch_testing.launch_test.main reads sys.argv directly (argparse).
    # Drop our own argv[0] and present launch_test as the program name.
    sys.argv = ["launch_test"] + sys.argv[1:]
    return lt_main()


if __name__ == "__main__":
    sys.exit(main())
