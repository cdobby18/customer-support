"""Prompt template registry for the LLM gateway.

Each template is a named, versioned message body using ``{placeholder}``
substitution. Rendering is strict: every placeholder must be supplied and
no unknown parameters are silently ignored, so prompt drift is caught early.
"""

import re


class PromptTemplateError(Exception):
    """Raised for unknown or mis-used templates/parameters."""


class PromptTemplate:
    def __init__(
        self,
        name: str,
        version: int,
        template: str,
        *,
        description: str = "",
    ) -> None:
        self.name = name
        self.version = version
        self.template = template
        self.description = description

    def render(self, **params: object) -> str:
        placeholders = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", self.template))
        provided = set(params)
        missing = placeholders - provided
        if missing:
            raise PromptTemplateError(
                f"template {self.name!r} missing parameters: {sorted(missing)}"
            )
        unexpected = provided - placeholders
        if unexpected:
            raise PromptTemplateError(
                f"template {self.name!r} got unexpected parameters: {sorted(unexpected)}"
            )
        return self.template.format(**params)


TRIAGE_SYSTEM_TEMPLATE = (
    "You are the Triage Agent for a customer support platform. Classify each "
    "incoming support request into a single structured JSON object using "
    "exactly these keys:\n"
    "- intent: one of account_access, billing, fraud, legal, technical_support, "
    "general_support\n"
    "- priority: one of normal, high, urgent\n"
    "- sentiment: one of neutral, frustrated, angry\n"
    "- confidence: a float between 0 and 1\n"
    "- recommended_team: one of billing, fraud, leadership, "
    "engineering_support, account_support, customer_support\n"
    "- requires_human_review: boolean; true for fraud, legal, billing, and "
    "account_access issues or anything escalation-worthy, otherwise false\n"
    "- summary: a concise one-sentence summary of the issue\n\n"
    "Do not invent facts. If information is missing, mark it as unknown and "
    "request clarification in the summary. Respond with JSON only.\n\n"
    "Customer message:\n{message}"
)

RESPONSE_DRAFT_TEMPLATE = (
    "You are the Response Agent for a customer support platform. Draft a "
    "concise, empathetic, and actionable customer-facing reply using only the "
    "approved knowledge base excerpts provided below. Follow these rules:\n"
    "- Be warm and professional; never invent facts or policies not present in "
    "the excerpts\n"
    "- Cite each source by number, for example: (Source 1), (Source 2)\n"
    "- If the excerpts are insufficient to answer confidently, state that a "
    "specialist will follow up\n"
    "- Keep the reply under 150 words\n"
    "- Do not include HTML, markdown links, or placeholder text\n\n"
    "Knowledge base excerpts:\n{excerpts}\n\n"
    "Customer message:\n{message}"
)

RESPONSE_DRAFT_JSON_TEMPLATE = (
    "You are the Response Agent for a customer support platform. Draft a "
    "concise, empathetic, and actionable customer-facing reply using only the "
    "approved knowledge base excerpts provided below. Follow these rules:\n"
    "- Be warm and professional; never invent facts or policies not present in "
    "the excerpts\n"
    "- Cite each source by number, for example: (Source 1), (Source 2)\n"
    "- If the excerpts are insufficient to answer confidently, suggest "
    "escalation instead of guessing\n"
    "- Keep the reply under 150 words\n"
    "- Do not include HTML, markdown links, or placeholder text\n\n"
    "Knowledge base excerpts:\n{excerpts}\n\n"
    "The customer message is provided in the user turn. Return a single JSON "
    "object with exactly these keys:\n"
    "- draft: your drafted reply, plain text under 150 words\n"
    "- confidence: a float between 0 and 1 estimating how confident you are "
    "that this answer resolves the issue\n"
    "- citations: an array of the source labels you actually used, for "
    "example: [Source 1]\n"
    "- escalate: a boolean; true when the reply cannot resolve the issue alone\n"
    "- reason: a one-sentence explanation of your recommendation\n\n"
    "Respond with JSON only."
)

ESCALATION_SUMMARY_TEMPLATE = (
    "You are the Escalation Agent for a customer support platform. Summarize "
    "the ticket below for a human reviewer. Include the core issue, customer "
    "tier and history context when provided, detected intent and sentiment, "
    "any guardrail or policy flags, and a recommended action. Keep it under "
    "120 words.\n\n"
    "Ticket details:\n{details}"
)

AGENT_ASSIST_TEMPLATE = (
    "You are the Agent Assist Agent helping a live support agent. Given the "
    "ticket history below, provide:\n"
    "- a one paragraph summary of the conversation\n"
    "- up to 3 suggested replies the agent could send\n"
    "- the team or specialist that should handle it, if any\n\n"
    "Ticket history:\n{history}"
)


TEMPLATES: dict[str, PromptTemplate] = {
    template.name: template
    for template in (
        PromptTemplate(
            "triage.classify",
            1,
            TRIAGE_SYSTEM_TEMPLATE,
            description="LLM-based triage classification (Task 4).",
        ),
        PromptTemplate(
            "response.draft",
            1,
            RESPONSE_DRAFT_TEMPLATE,
            description="KB-grounded customer reply draft (Task 3).",
        ),
        PromptTemplate(
            "response.draft_json",
            1,
            RESPONSE_DRAFT_JSON_TEMPLATE,
            description="Structured KB-grounded reply draft: draft + confidence + citations + escalation (Task 3).",
        ),
        PromptTemplate(
            "escalation.summary",
            1,
            ESCALATION_SUMMARY_TEMPLATE,
            description="Context summary for human reviewers (Task 6).",
        ),
        PromptTemplate(
            "agent_assist.suggest",
            1,
            AGENT_ASSIST_TEMPLATE,
            description="Ticket summary + suggested replies (Task 7).",
        ),
    )
}


def get_template(name: str) -> PromptTemplate:
    template = TEMPLATES.get(name)
    if template is None:
        known = sorted(TEMPLATES)
        raise PromptTemplateError(f"unknown template {name!r}; known: {known}")
    return template