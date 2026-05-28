import os
import sys

def get_process_details():
    pids = [p for p in os.listdir('/proc') if p.isdigit()]
    target_pid = None
    for pid in pids:
        try:
            cmdline = open(f'/proc/{pid}/cmdline').read().replace('\x00', ' ')
            if 'recover_web.py' in cmdline:
                target_pid = pid
                print(f"Found process PID {pid} with cmdline: {cmdline}")
                break
        except Exception:
            continue
            
    if not target_pid:
        print("recover_web.py process NOT found.")
        return
        
    try:
        status = open(f'/proc/{target_pid}/status').read()
        print("\n--- STATUS ---")
        print(status)
    except Exception as e:
        print(f"Error reading status: {e}")
        
    try:
        # Check files/fds
        print("\n--- FILE DESCRIPTORS ---")
        fd_dir = f'/proc/{target_pid}/fd'
        for fd in os.listdir(fd_dir):
            target = os.readlink(os.path.join(fd_dir, fd))
            print(f"  {fd} -> {target}")
    except Exception as e:
        print(f"Error reading file descriptors: {e}")

if __name__ == "__main__":
    get_process_details()
