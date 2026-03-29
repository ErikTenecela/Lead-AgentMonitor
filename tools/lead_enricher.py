"""
lead_enricher.py
Enriches a qualifying lead with business intelligence before sending to Telegram.

Exercise: Optional fields, business-value AI, sales intelligence in schemas.

New concepts:
- Optional fields: not everything appears in every post
- Hallucination prevention: don't force fields Claude can't fill honestly
- AI as a sales assistant: adding actionable context, not just classification
"""

import os
import anthropic
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── System prompt ─────────────────────────────────────────────────────────────
# This prompt teaches Claude to think like a contractor's sales assistant.
# Notice how specific the business context is — the more context, the better output.

SYSTEM_PROMPT = """You are a sales intelligence assistant for an outdoor home services
contractor in Fairfield County, CT. Services: chimney repair, masonry, patios,
walkways, driveways, retaining walls, junk removal, tree removal, landscaping,
painting, power washing.

When a qualifying lead comes in, your job is to enrich it with actionable
business intelligence so the contractor knows exactly how to respond.

Think like an experienced contractor reading this post:
- What is this homeowner really trying to accomplish?
- What questions should be asked before quoting?
- Are there any signals that this could be a problem client?
- What's the best opening line when calling?

Budget signals to watch for:
- Positive: "price is not an issue", "quality matters", "need it done right"
- Negative: "looking for cheapest", "tight budget", "just need basic"
- Neutral: no mention of budget

HOA note: townhouses, condos, planned communities often require HOA approval
for exterior work. Flag this so contractor asks before starting work.

Permit note: structural work (retaining walls, foundations, large driveways)
often requires permits in CT. Flag when likely needed.

Only fill optional fields when the post gives you enough real information.
Never guess or hallucinate details not present in the post."""


# ── Tool schema ───────────────────────────────────────────────────────────────
# Key lesson: required vs optional fields
# required = always present, model must fill these
# optional = only filled when post has enough real information
# This prevents hallucination on sparse posts

ENRICHMENT_TOOL = {
    "name": "enrich_lead",
    "description": "Add business intelligence to a qualifying lead.",
    "input_schema": {
        "type": "object",
        "properties": {

            # ── Required fields ── always present ────────────────────────────

            # Your "type of work" field — how big is this job
            "job_size": {
                "type": "string",
                "enum": ["small", "medium", "large"],
                "description": "small=under $1k, medium=$1k-$5k, large=$5k+"
            },

            # Your "improvements vs investing" field — why do they want this
            # enum you designed: investment, urgent_repair, upgrade, unknown
            "homeowner_intent": {
                "type": "string",
                "enum": ["investment", "urgent_repair", "upgrade", "unknown"],
                "description": (
                    "investment=flipping/renting, urgent_repair=something broke, "
                    "upgrade=cosmetic improvement, unknown=unclear"
                )
            },

            # Most valuable field — what to actually say when calling
            "suggested_opener": {
                "type": "string",
                "description": (
                    "One sentence the contractor says when the homeowner picks up. "
                    "Natural, not salesy. Specific to their situation."
                )
            },

            # ── Optional fields ── only filled when post has enough info ──────
            # These are NOT in the required list below
            # If the post doesn't mention budget, Claude leaves budget_signal out
            # This prevents hallucination

            # Your "do they have funds" field
            "budget_signal": {
                "type": "string",
                "enum": ["strong", "neutral", "weak"],
                "description": "Only include if post mentions budget. strong=quality focused, weak=price shopping"
            },

            # Your "HOA approval" field
            "hoa_required": {
                "type": "boolean",
                "description": "Only include if post signals HOA community (condo, townhouse, planned community)"
            },

            # Your "blueprints required" field
            "blueprints_needed": {
                "type": "boolean",
                "description": "Only include if job scope suggests blueprints needed (large structural work)"
            },

            # Your "safety regulations" field
            "permit_likely": {
                "type": "boolean",
                "description": "Only include if job type typically requires CT permits"
            },

            # Your "deadline" field
            "deadline": {
                "type": "string",
                "description": "Only include if post mentions a specific timeframe or urgency window"
            },

            # Competitor mentioned — good to know before calling
            "competitor_mentioned": {
                "type": "boolean",
                "description": "Only include if post mentions they already contacted another contractor"
            },

            # Discovery question to ask on the call
            "key_question": {
                "type": "string",
                "description": "The single most important question to ask before quoting"
            }
        },

        # Only 3 required — everything else is optional
        # Claude fills optional fields only when the post gives real information
        "required": ["job_size", "homeowner_intent", "suggested_opener"]
    }
}


# ── Main function ─────────────────────────────────────────────────────────────

def enrich_lead(post: dict) -> dict | None:
    """
    Enrich a qualifying lead with business intelligence.
    Returns enrichment dict or None on failure.
    """

    user_message = (
        f"Post text: {post.get('text', '')}\n"
        f"Town: {post.get('town', 'unknown')}\n"
        f"Work type: {post.get('work_type', 'unknown')}\n"
        f"Score: {post.get('score', 0)}/10\n"
        f"Urgency: {post.get('urgency', 'unknown')}\n\n"
        f"Enrich this lead with business intelligence."
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            tools=[ENRICHMENT_TOOL],
            tool_choice={"type": "tool", "name": "enrich_lead"},
            messages=[{"role": "user", "content": user_message}]
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "enrich_lead":
                return block.input

        return None

    except Exception as e:
        print(f"[enricher] API error: {e}")
        return None


# ── Test runner ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_leads = [
        {
            "text": "My chimney has crumbling mortar and two bricks fell off last week. "
                    "Need someone out ASAP — worried about safety. Price is not an issue, "
                    "just need it done right.",
            "town": "Westport",
            "work_type": "chimney repair",
            "score": 9,
            "urgency": "high"
        },
        {
            "text": "Looking for the cheapest quote to pour a basic concrete driveway. "
                    "Live in Willow Creek Condos in Norwalk. Nothing fancy, just functional. "
                    "HOA already approved the project.",
            "town": "Norwalk",
            "work_type": "driveway",
            "score": 7,
            "urgency": "medium"
        },
        {
            "text": "Buying a fixer upper in Bridgeport and want to completely redo the "
                    "backyard — new patio, retaining wall, and landscaping. Big project. "
                    "Already got one quote from Greenleaf Masonry, looking for competitive bids. "
                    "Need permits sorted too.",
            "town": "Bridgeport",
            "work_type": "patio + retaining wall",
            "score": 8,
            "urgency": "medium"
        },
    ]

    for lead in test_leads:
        print(f"Lead: {lead['work_type']} in {lead['town']} ({lead['score']}/10)")
        print("-" * 50)
        result = enrich_lead(lead)
        if result:
            # Required fields — always present
            print(f"Job size:         {result['job_size']}")
            print(f"Intent:           {result['homeowner_intent']}")
            print(f"Opener:           {result['suggested_opener']}")

            # Optional fields — only print if they exist
            # .get() returns None if field wasn't filled — no crash
            if result.get("budget_signal"):
                print(f"Budget signal:    {result['budget_signal']}")
            if result.get("hoa_required") is not None:
                print(f"HOA required:     {result['hoa_required']}")
            if result.get("permit_likely") is not None:
                print(f"Permit likely:    {result['permit_likely']}")
            if result.get("blueprints_needed") is not None:
                print(f"Blueprints:       {result['blueprints_needed']}")
            if result.get("deadline"):
                print(f"Deadline:         {result['deadline']}")
            if result.get("competitor_mentioned") is not None:
                print(f"Competitor:       {result['competitor_mentioned']}")
            if result.get("key_question"):
                print(f"Key question:     {result['key_question']}")
        else:
            print("ERROR: enrichment failed")
        print()
