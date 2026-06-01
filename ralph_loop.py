import asyncio
import os
import sys
import re
import subprocess
from pathlib import Path
from google.antigravity import Agent, LocalAgentConfig
from google.antigravity.hooks import policy

def get_next_task() -> str | None:
    prd_path = Path("PRD.md")
    progress_path = Path("progress.txt")
    
    if not prd_path.exists():
        print("Error: PRD.md does not exist.")
        return None
        
    with open(prd_path, "r", encoding="utf-8") as f:
        prd_content = f.read()
        
    tasks = re.findall(r"\d+\.\s+\*\*Task\s+\d+\s+\(([^)]+)\):\*\*([^\n]+)", prd_content)
    if not tasks:
        # Fallback to general task regex
        tasks = re.findall(r"-\s+Task\s+\d+\s+\(([^)]+)\):\s+([^\n]+)", prd_content)
        
    if not tasks:
        print("No tasks found in PRD.md")
        return None
        
    progress_content = ""
    if progress_path.exists():
        with open(progress_path, "r", encoding="utf-8") as f:
            progress_content = f.read()
            
    for name, desc in tasks:
        # If the task name is not mentioned as completed in the logs
        search_str = f"Completed Task {len(tasks)}" # Fallback / placeholder
        if name not in progress_content and "Completed " + name not in progress_content:
            return f"Task: {name} - Description: {desc.strip()}"
            
    return None

async def run_loop_step(task: str) -> bool:
    print(f"\n[Ralph Loop] Next pending task identified:\n  {task}\n")
    print("[Ralph Loop] Initializing Antigravity Agent...")
    
    prompt = f"""
We are running a Ralph Loop autonomous self-improvement cycle on the codebase.
The next task to complete is:
{task}

Please perform the following steps:
1. Research the codebase to locate files that need changes.
2. Modify the code to implement the task.
3. Verify your changes by running pytest (`uv run pytest`). Make sure all tests pass.
4. Once tests pass, append a completion log entry to `progress.txt` describing your changes.
5. Do not run any other tasks.
"""

    config = LocalAgentConfig(
        system_instructions="You are an autonomous software developer agent. Complete the requested task, run pytest to verify, and append progress.",
        policies=[policy.allow_all()],
    )
    
    try:
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            print("[Ralph Loop] Agent Reasoning & Response:")
            async for chunk in response:
                print(chunk, end="", flush=True)
            print()
        return True
    except Exception as e:
        print(f"[Ralph Loop] Error running agent turn: {e}")
        return False

def verify_and_commit() -> bool:
    print("[Ralph Loop] Running pytest validation...")
    res = subprocess.run(["uv", "run", "pytest"], capture_output=True, text=True)
    if res.returncode != 0:
        print("[Ralph Loop] ✘ Test suite failed! Aborting commit.")
        print(res.stderr or res.stdout)
        return False
        
    print("[Ralph Loop] ✔ All tests passed! Committing changes...")
    subprocess.run(["git", "add", "-A"])
    commit_res = subprocess.run(["git", "commit", "-m", "chore: Ralph Loop iteration completed"], capture_output=True, text=True)
    print(commit_res.stdout)
    return True

async def main():
    max_iterations = 5
    for i in range(max_iterations):
        print(f"\n=================== RALPH LOOP ITERATION {i+1}/{max_iterations} ===================")
        task = get_next_task()
        if not task:
            print("[Ralph Loop] ✔ All tasks are completed. Stopping loop.")
            
            # Update progress status to COMPLETED
            progress_path = Path("progress.txt")
            if progress_path.exists():
                with open(progress_path, "r", encoding="utf-8") as f:
                    content = f.read()
                content = content.replace("STATUS: IN_PROGRESS", "STATUS: COMPLETED")
                with open(progress_path, "w", encoding="utf-8") as f:
                    f.write(content)
                subprocess.run(["git", "add", "progress.txt"])
                subprocess.run(["git", "commit", "-m", "chore: Finalize Ralph Loop status to completed"])
            break
            
        success = await run_loop_step(task)
        if not success:
            print("[Ralph Loop] Agent execution failed.")
            sys.exit(1)
            
        # Verify and commit the iteration
        if not verify_and_commit():
            sys.exit(1)
            
        # Sleep briefly to ensure database states and file system handles are flushed
        await asyncio.sleep(2)

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
