import sys
import os

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), "backend"))

try:
    print("Importing status_manager...")
    from status_manager import status_manager
    
    print("Calling get_system_info()...")
    info = status_manager.get_system_info()
    print(f"Result: {info}")
    
except Exception as e:
    import traceback
    traceback.print_exc()
