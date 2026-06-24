# AMP Mem0 REST Wrapper

An Agent Memory Protocol (AMP) v1.1 / v1.2-draft conformant HTTP/REST wrapper for Mem0. It allows bots and applications to interact with Mem0 storage (e.g. Qdrant or local) using standard network endpoints rather than stdio subprocesses.

## Installation

```bash
pip install -e .
```

## Running the Server

Start the REST wrapper server:
```bash
amp-mem0-rest-wrapper --port 8000 --host 0.0.0.0
```

## Configuration

You can run the server in three modes:

### 1. Production Mode with Qdrant (Self-Hosted)
Pass your production Mem0 configuration YAML/JSON file containing Qdrant credentials:
```bash
amp-mem0-rest-wrapper --config-path /path/to/your-qdrant-config.yaml
```

**Example YAML (`config.yaml`):**
```yaml
vector_store:
  provider: qdrant
  config:
    host: qdrant-service.default.svc.cluster.local
    port: 6333
    collection_name: memories
llm:
  provider: openai
  config:
    api_key: your-openai-key
    model: gpt-4o
embedder:
  provider: openai
  config:
    api_key: your-openai-key
    model: text-embedding-3-small
```

### 2. Platform Mode (Remote Cloud)
Configure your Mem0 Platform API key in the environment:
```bash
export MEM0_API_KEY="your-api-key"
amp-mem0-rest-wrapper
```

### 3. Local Default Mode (Offline)
Runs locally using Chroma DB and mock model configurations (meaning zero external APIs are consumed):
```bash
amp-mem0-rest-wrapper --storage-path ~/.amp/mem0
```

## REST API Endpoints

Conforms to Appendix C.1 of the AMP specification:
* `POST /v1/memories` -> Store memory (`amp.encode`)
* `POST /v1/memories/recall` -> Search memories (`amp.recall`)
* `DELETE /v1/memories/{id}` -> Forget memory (`amp.forget`)
* `PATCH /v1/memories/{id}` -> Update memory (`amp.update`)
* `GET /v1/memories/stats` -> Stats (`amp.stats`)
* `PUT /v1/memories/{id}/pin` -> Returns `not_supported`
* `POST /v1/memories/consolidate` -> Returns `not_supported`
* `POST /v1/memories/batch_encode` -> Batch store memories (`amp.batch_encode`)
