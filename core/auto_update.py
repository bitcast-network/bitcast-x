import subprocess
import os
import time


def _run_git(cmd, cwd):
    """Run a git command and return stripped stdout, or None on failure."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip()
        print(f"Auto-update skipped: git command failed ({' '.join(cmd)}): {error}")
        return None
    output = result.stdout.strip()
    if not output:
        print(f"Auto-update skipped: git command returned empty output ({' '.join(cmd)})")
        return None
    return output

def should_update_local(local_commit, remote_commit):
    return local_commit != remote_commit


def run_auto_update(neuron_type):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(script_dir, ".."))

    current_branch = _run_git(["git", "rev-parse", "--abbrev-ref", "HEAD"], project_root)
    if not current_branch:
        return

    local_commit = _run_git(["git", "rev-parse", "HEAD"], project_root)
    if not local_commit:
        return

    fetch_result = subprocess.run(
        ["git", "fetch", "origin"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if fetch_result.returncode != 0:
        error = (fetch_result.stderr or fetch_result.stdout).strip()
        print(f"Auto-update skipped: git fetch failed: {error}")
        return

    remote_commit = _run_git(["git", "rev-parse", f"origin/{current_branch}"], project_root)
    if not remote_commit:
        return

    if should_update_local(local_commit, remote_commit):
        print("Local repo is not up-to-date. Updating...")
        reset_cmd = ["git", "reset", "--hard", remote_commit]
        process = subprocess.Popen(reset_cmd, cwd=project_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate()

        if process.returncode != 0:
            print("Error in updating:", (error or b"").decode(errors="replace").strip())
        else:
            print(f"Updated local repo to latest version: {remote_commit}")

            print("Running the autoupdate steps...")
            # Run setup script with venv activation
            project_parent = os.path.abspath(os.path.join(project_root, ".."))
            venv_path = f"{project_parent}/venv_bitcast_x"
            setup_cmd = f"source {venv_path}/bin/activate && {project_root}/scripts/setup_env.sh"
            subprocess.run(setup_cmd, shell=True, executable='/bin/bash')

            time.sleep(20)
            print("Finished running the autoupdate steps")
            print("Restarting neuron")
            # Run start script with venv activation
            start_cmd = f"source {venv_path}/bin/activate && {project_root}/scripts/run_{neuron_type}.sh"
            subprocess.run(start_cmd, shell=True, executable='/bin/bash')
    else:
        print("Repo is up-to-date.")