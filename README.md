# AI Customer Support Automation

A production-ready starter blueprint for an intelligent customer support automation platform that combines conversational AI, ticket triage, routing, workflow automation, and human agent assist.

## Overview

This project is designed for an AI-first support system that can:

- accept customer requests from email, chat, WhatsApp, or web forms
- classify issue type and urgency automatically
- retrieve answers from a knowledge base using RAG
- draft customer-facing responses in brand tone
- escalate risky or unresolved issues to human agents
- track performance with SLA, CSAT, and resolution metrics

## Core Features

- Ticket intake and normalization
- Intent and sentiment classification
- Smart routing and prioritization
- FAQ / policy retrieval with semantic search
- AI-generated response drafting
- Human approval and escalation logic
- Analytics and feedback loop
- CRM / support system integrations

## Suggested Stack

- Frontend: Next.js / React
- Backend: FastAPI / Node.js
- AI Layer: OpenAI or Azure OpenAI
- Vector DB: Pinecone / Qdrant / Weaviate / FAISS
- Database: PostgreSQL
- Cache / Queue: Redis + Celery or Temporal
- Monitoring: Langfuse, OpenTelemetry, Prometheus, Grafana

## MVP Roadmap

1. Build ticket intake and storage
2. Add triage + intent detection
3. Integrate knowledge retrieval and grounded responses
4. Add human review for escalations
5. Connect CRM and communication channels
6. Add analytics dashboard and evaluation metrics

## Run Locally

### Prerequisites

- Python 3.14+
- Node.js 18+
- Docker Desktop only if you want PostgreSQL instead of the default SQLite database

### 1. Install backend dependencies

From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

PowerShell activation is optional. Using `.venv\Scripts\python.exe` avoids
interpreter mismatches.

### 2. Start the API with SQLite

SQLite is the default and requires no database setup:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Keep this terminal running. API URLs:

- Health: `http://127.0.0.1:8000/health`
- Swagger docs: `http://127.0.0.1:8000/docs`

### 3. Start the frontend

Open a second terminal in the project root:

```powershell
Push-Location frontend
npm install
npm run dev -- --host 127.0.0.1
```

Open `http://127.0.0.1:5173/` in your browser. To stop the frontend terminal,
press `Ctrl+C`, then run `Pop-Location` if needed.

### 4. Test the application

Register a customer from **Create account**, then create a ticket from the
dashboard. Run the backend tests from another terminal:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected current result: `37 passed`.

For an end-to-end API smoke test while the API is running:

```powershell
.\scripts\smoke_test.ps1
```
## Success Metrics

- reduced first response time
- improved CSAT
- lower manual workload
- higher resolution rate for common issues
- better ticket routing accuracy
- lower escalation failure rate
