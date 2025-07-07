# Skript zum Installieren von python-dotenv
# Ausführen mit: python install_dotenv.py
import subprocess
import sys

def install_dotenv():
    subprocess.check_call([sys.executable, "-m", "pip", "install", "python-dotenv"])

if __name__ == "__main__":
    install_dotenv()
    print("python-dotenv wurde installiert.")
