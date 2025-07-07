import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Get the Discord bot token and guild ID from environment variables
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))

# ...existing code...