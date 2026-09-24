# Prompt Engineering Prompt: AI Customer Support Automation

## Prompt

You are an expert AI product engineer, systems architect, and prompt designer building an AI Customer Support Automation platform.

Create a production-ready project blueprint for an intelligent support system that helps businesses handle customer inquiries automatically across email, chat, web forms, and messaging channels. The project must combine AI reasoning, retrieval-augmented generation, workflow automation, and human escalation logic.

### Objectives

- Reduce support workload by automating repetitive customer requests.
- Route tickets to the correct team or agent using business rules and AI classification.
- Generate accurate, branded responses grounded in internal knowledge bases and policies.
- Detect urgency, sentiment, and customer risk for timely intervention.
- Provide agent assist tools for faster resolution and better consistency.
- Maintain human approval for high-risk, refund, legal, or policy-sensitive scenarios.
- Track performance using SLA, CSAT, resolution time, and deflection metrics.

### Requirements

Build the project with:

- A modern AI-first architecture with modular services
- LLM-based ticket triage, intent recognition, and response drafting
- Retrieval over FAQs, policies, product docs, and troubleshooting guides
- Safe guardrails, prompt containment, and compliance checks
- Workflow automation for routing, tagging, reminders, and escalations
- Authentication, role-based access, and secure API handling
- Integration hooks for Zendesk, Freshdesk, Intercom, HubSpot, Slack, email, and chat
- Observability with logs, traces, metrics, and evaluation dashboards
- Human-in-the-loop approval flows for sensitive issues

### System Design Expectations

- Multi-layer architecture: frontend, API layer, orchestration layer, AI agents, integrations, and data layer
- Support for asynchronous background jobs and event-driven processing
- Clear separation between inference, retrieval, policy enforcement, and ticket workflow
- Model evaluation pipeline with labeled support cases and feedback loops
- Error handling, fallback responses, and escalation paths
- Strong prompt design for classification, summarization, response generation, and triage

### Technical Stack

Use a practical and scalable stack such as:

- Backend: FastAPI or Node.js
- Frontend: Next.js or React
- Database: PostgreSQL
- Cache/Queue: Redis + Celery or Temporal
- Vector search: Pinecone, Weaviate, Qdrant, or FAISS
- LLMs: OpenAI, Azure OpenAI, or open-source models
- Monitoring: Langfuse, OpenTelemetry, Prometheus, Grafana

### Deliverables

Provide:

1. A complete project architecture overview
2. Folder structure and implementation plan
3. AI agent roles and responsibilities
4. Core prompts for ticket classification, triage, policy compliance, and reply generation
5. Secure and production-ready API design
6. Workflow automation and escalation logic
7. Example evaluation metrics and success dashboards
8. A development roadmap from MVP to production

### Output Style

The response should be structured, technical, and implementation-focused. Use clear section headings, concise descriptions, and realistic engineering choices.

### Final Instruction

Generate the full project plan and starter code structure for an AI Customer Support Automation system that is realistic, scalable, and suitable for an enterprise support environment.
