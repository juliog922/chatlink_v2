FROM ollama/ollama:latest

ENV OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_KEEP_ALIVE=24h

# Don’t start Ollama + pull during build; just ship the server.
EXPOSE 11434
CMD ["serve"]
