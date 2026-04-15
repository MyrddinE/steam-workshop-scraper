import subprocess
import sys
import os

def run_tests():
    """
    Helper script to run pytest with coverage and correct environment settings.
    """
    # Ensure we are in the project root
    project_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_root)

    cmd = [
        sys.executable, "-m", "pytest",
        "--cov=src",
        "--cov-report=term-missing",
        "tests/"
    ]
    
    # Add any extra arguments passed to this script
    if len(sys.argv) > 1:
        cmd.extend(sys.argv[1:])

    print(f"Executing: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=True)
        sys.exit(result.returncode)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)

if __name__ == "__main__":
    run_tests()
