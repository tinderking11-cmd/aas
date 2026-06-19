import os
import subprocess
import sys
import time
from pathlib import Path

from otp_router import number_bot_is_running


NUMBER_BOT_PATH = Path(__file__).with_name("namberbot.py")


def ask_and_maybe_start_number_bot():
    answer = input("👉 Number bot-ও চালু করতে চান? (Y/N): ").strip().lower()
    if answer not in ("y", "yes"):
        return False
    if number_bot_is_running():
        print("[✅] Number bot already running.")
        return True
    kwargs = {"cwd": str(NUMBER_BOT_PATH.parent)}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    subprocess.Popen([sys.executable, str(NUMBER_BOT_PATH)], **kwargs)
    time.sleep(2)
    print("[✅] Number bot start requested.")
    return True
