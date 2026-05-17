"""Run the full automated verification suite (Stage 5.1).

Usage (from project root):
  ../venv/Scripts/python.exe -m tests.run_all
"""
import subprocess
import sys

SUITES = [
    "tests.test_stage1",
    "tests.test_stage2",
    "tests.test_stage3",
    "tests.test_stage4",
    "tests.test_delivery",
    "tests.test_reliability",
    "tests.test_calibration",
    "tests.test_e2e_pipeline",
]


def main() -> None:
    failed: list[str] = []
    for mod in SUITES:
        print(f"\n=== {mod} ===")
        rc = subprocess.run([sys.executable, "-m", mod]).returncode
        if rc != 0:
            failed.append(mod)
    print("\n" + "=" * 40)
    if failed:
        print("FAILED: " + ", ".join(failed))
        sys.exit(1)
    print(f"ALL {len(SUITES)} SUITES PASSED")


if __name__ == "__main__":
    main()
