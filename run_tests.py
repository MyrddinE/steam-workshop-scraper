import subprocess
import sys
import os

def run_tests():
    project_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_root)

    cmd = [sys.executable, "-m", "pytest", "tests/"]
    
    args = sys.argv[1:]
    # Interpret convenience shortcuts
    if "--quick" in args:
        args.remove("--quick")
        cmd += ["-k", "not fuzz and not test_seed_database_halts"]
    if "--unit" in args:
        args.remove("--unit")
        cmd += [
            "-k", "not fuzz and not test_seed_database_halts",
            "--ignore=tests/test_system_e2e.py",
            "--ignore=tests/test_web_scraper_live.py",
            "--ignore=tests/test_api_reachability.py",
        ]

    cmd.extend(args)

    print(f"Executing: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=True)
        sys.exit(result.returncode)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)

if __name__ == "__main__":
    run_tests()
