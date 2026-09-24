# AGENTS.md

## Mission

Build an AI-powered customer support automation system that reduces repetitive manual work, accelerates resolution, and preserves human oversight for sensitive or high-risk cases.

## Agent Roles

### 1. Intake Agent
- Receives incoming support requests from chat, email, web forms, or messaging platforms.
- Extracts customer context, issue type, urgency, and channel metadata.
- Normalizes ticket content into a structured internal format.

### 2. Triage Agent
- Classifies the request by category, severity, priority, and team.
- Uses rules, ML classification, and LLM reasoning.
- Determines whether the issue is routine, escalated, or needs agent review.

### 3. Knowledge Agent
- Retrieves answers from FAQs, policies, product docs, and troubleshooting guides.
- Reranks relevant documents using semantic search.
- Grounds responses in approved sources.

### 4. Response Agent
- Drafts customer-facing responses in the brand voice.
- Produces concise, empathetic, and actionable replies.
- Includes fallback messaging when confidence is low.

### 5. Escalation Agent
- Detects risky or sensitive issues requiring human intervention.
- Routes cases to billing, engineering, fraud, or leadership teams.
- Tracks SLA deadlines and reminder triggers.

### 6. Agent Assist Agent
- Helps live agents by summarizing ticket history and suggesting actions.
- Finds similar resolved issues and recommended responses.

### 7. Evaluation Agent
- Collects CSAT, escalation, and resolution data.
- Measures model quality and identifies retraining candidates.

## Core Workflow

1. Ticket arrives from a channel integration.
2. Intake agent collects and normalizes data.
3. Triage agent classifies intent, severity, and ownership.
4. Knowledge agent retrieves policy and issue context.
5. Response agent drafts the answer or action.
6. Guardrail layer validates compliance and safety.
7. If approved, send the response or trigger automation.
8. If risky, escalate to a human agent.
9. Feedback loop updates analytics and retraining pipelines.

## Design Principles

- Human oversight for high-risk cases
- Retrieval grounded in trusted sources
- Prompt and policy guardrails before sending responses
- Event-driven automation for speed and reliability
- Audit trails for decisions and actions
- Measurable impact on CSAT, deflection, and resolution time

## Example Agent Prompt

You are the Triage Agent for a customer support platform. Your task is to classify each incoming ticket by intent, urgency, sentiment, and recommended team. Return a structured JSON object with issue_type, priority, sentiment, is_escalation_needed, recommended_team, confidence, and summary. Do not invent facts. If information is missing, mark it as unknown and request clarification if necessary.

## Implementation Rules

- Keep each AI module isolated with clear interfaces.
- Log prompt inputs and outputs for debugging and evaluation.
- Use structured outputs for classification and routing.
- Validate before sending customer responses.
- Prefer deterministic business rules for policy-sensitive tasks.
