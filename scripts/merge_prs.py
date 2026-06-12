#!/usr/bin/env python3
import json
import subprocess
import sys
import os

def run_cmd(cmd, cwd=None, check=True):
    print(f"Running: {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and res.returncode != 0:
        print(f"Command failed! Stderr: {res.stderr}")
        raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)
    return res

def resolve_palette_md_conflicts(filepath):
    if not os.path.exists(filepath):
        return
    with open(filepath, "r") as f:
        content = f.read()
    
    # Remove git conflict markers completely from the text
    lines = content.splitlines()
    clean_lines = []
    for line in lines:
        if any(line.startswith(marker) for marker in ["<<<<<<<", "=======", ">>>>>>>"]):
            continue
        clean_lines.append(line)
    
    clean_content = "\n".join(clean_lines)
    
    # Split by "## " to get individual learning entries
    parts = clean_content.split("## ")
    header = parts[0]  # Anything before the first entry (should be empty or introductory)
    entries = parts[1:]
    
    unique_entries = []
    seen = set()
    for entry in entries:
        entry_lines = entry.strip().splitlines()
        if not entry_lines:
            continue
        # Deduplicate based on the title line (date + title)
        title = entry_lines[0].strip()
        if title not in seen:
            seen.add(title)
            unique_entries.append(entry.strip())
            
    # Reassemble
    new_content = header
    if unique_entries:
        new_content += "## " + "\n\n## ".join(unique_entries) + "\n"
        
    with open(filepath, "w") as f:
        f.write(new_content)
    print(f"Resolved palette.md conflicts. Retained {len(unique_entries)} unique entries.")

def main():
    repo_dir = "/Users/sanketmane/Codes/Growin App"
    
    # Get the list of all open PRs sorted by number (oldest first)
    out = run_cmd(["gh", "pr", "list", "--limit", "100", "--json", "number,title,headRefName"], cwd=repo_dir)
    prs = json.loads(out.stdout)
    prs.sort(key=lambda x: x["number"])
    
    print(f"Found {len(prs)} open PRs to process.")
    
    for pr in prs:
        num = pr["number"]
        title = pr["title"]
        ref = pr["headRefName"]
        
        print(f"\n==================================================")
        print(f"Processing PR #{num}: {title}")
        print(f"Branch: {ref}")
        print(f"==================================================")
        
        # Verify state is still OPEN
        view_out = run_cmd(["gh", "pr", "view", str(num), "--json", "state"], cwd=repo_dir)
        state_data = json.loads(view_out.stdout)
        if state_data.get("state") != "OPEN":
            print(f"PR #{num} is {state_data.get('state')}. Skipping.")
            continue
            
        # Try direct merge
        merge_res = run_cmd(["gh", "pr", "merge", str(num), "--merge", "--admin"], cwd=repo_dir, check=False)
        if merge_res.returncode == 0:
            print(f"PR #{num} merged successfully directly.")
            continue
            
        print(f"Direct merge failed for PR #{num}. Attempting checkout and rebase/merge.")
        
        # Checkout the branch
        run_cmd(["git", "checkout", ref], cwd=repo_dir)
        
        # Try merging main
        merge_main_res = run_cmd(["git", "merge", "main", "--no-edit"], cwd=repo_dir, check=False)
        if merge_main_res.returncode != 0:
            print("Merge conflict encountered on checkout branch.")
            
            # Check which files have conflicts
            status_res = run_cmd(["git", "status", "--porcelain"], cwd=repo_dir)
            conflicting_files = []
            for line in status_res.stdout.splitlines():
                if line.startswith("UU ") or line.startswith("AA ") or line.startswith("both modified:"):
                    parts = line.split()
                    conflicting_files.append(parts[-1])
                elif "both modified:" in line:
                    parts = line.split("both modified:")
                    conflicting_files.append(parts[-1].strip())
            
            print(f"Conflicting files: {conflicting_files}")
            
            # If only .Jules/palette.md has conflict, resolve it automatically
            palette_path = ".Jules/palette.md"
            if len(conflicting_files) == 1 and conflicting_files[0] == palette_path:
                print("Only .Jules/palette.md has conflicts. Resolving automatically...")
                resolve_palette_md_conflicts(os.path.join(repo_dir, palette_path))
                run_cmd(["git", "add", palette_path], cwd=repo_dir)
                run_cmd(["git", "commit", "--no-edit"], cwd=repo_dir)
                run_cmd(["git", "push", "origin", ref], cwd=repo_dir)
            else:
                # If there are other conflicts, stop and report them so the agent/user can resolve
                print(f"CRITICAL: Non-automatic conflicts in {conflicting_files}. Exiting so they can be resolved.")
                sys.exit(1)
        else:
            print("Merged main into branch cleanly. Pushing updates...")
            run_cmd(["git", "push", "origin", ref], cwd=repo_dir)
            
        # Switch back to main and try merging again
        run_cmd(["git", "checkout", "main"], cwd=repo_dir)
        
        # Try merging PR again
        print(f"Retrying merge of PR #{num} after update...")
        retry_res = run_cmd(["gh", "pr", "merge", str(num), "--merge", "--admin"], cwd=repo_dir)
        print(f"PR #{num} retried and merged successfully.")
        
    print("\nAll PRs processed successfully!")

if __name__ == "__main__":
    main()
