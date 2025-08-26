# src/ai/pipeline.py
from langchain_ollama import ChatOllama

def build_chat(ollama_url: str | None = None) -> ChatOllama:
    return ChatOllama(
        model="llama3",
        temperature=0.0,
        repeat_penalty=1.1,
        num_predict=256,
        base_url=ollama_url or None,
    )