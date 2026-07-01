import os

from openai import OpenAI

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/v1")
client = OpenAI(base_url=OLLAMA_URL, api_key="ollama")

