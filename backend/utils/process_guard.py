"""
Process Guard Utility
Ensures that a process exits if its parent process dies or if it is orphaned.
This is crucial for preventing zombie processes when running MCP servers or subprocesses.
"""
import os
import sys
import time
import threading
import logging

# Configure basic logging if not already configured
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("process_guard")

def start_parent_watchdog(interval: float = 3.0):
    """
    Starts a daemon thread that monitors the parent process.
    If the parent process ID changes (indicating reparenting to init/launchd)
    or the parent process ceases to exist, this process will force exit.
    """
    try:
        initial_ppid = os.getppid()
        
        def watchdog_loop():
            while True:
                try:
                    current_ppid = os.getppid()
                    
                    # Check 1: Has the parent changed? 
                    # On many *nix, orphan adoption changes PPID to 1 (or other init).
                    if current_ppid != initial_ppid:
                        # Log to stderr to ensure it's captured even if logging is weird
                        sys.stderr.write(f"[ProcessGuard] Parent changed ({initial_ppid} -> {current_ppid}). Terminating.\n")
                        os._exit(0)
                    
                    # Check 2: Is the parent still alive?
                    # Sending signal 0 checks for existence without killing.
                    try:
                        os.kill(initial_ppid, 0)
                    except OSError:
                        sys.stderr.write(f"[ProcessGuard] Parent {initial_ppid} is dead. Terminating.\n")
                        os._exit(0)
                        
                except Exception as e:
                    # Failsafe
                    sys.stderr.write(f"[ProcessGuard] Error: {e}\n")
                
                time.sleep(interval)

        thread = threading.Thread(target=watchdog_loop, daemon=True, name="ParentWatchdog")
        thread.start()
        # logger.info(f"Process guard started. Monitoring parent PID {initial_ppid}")
        
    except Exception as e:
        sys.stderr.write(f"Failed to start process guard: {e}\n")
