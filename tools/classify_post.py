"""
classify_post.py
Uses Claude Haiku to score a post's relevance for outdoor/masonry services.

Uses the Anthropic tool_use API to enforce structured output at the API level.
This guarantees the response always matches the expected schema — no regex needed.

Usage:
    python classify_post.py  # runs sample classifications
"""

import os
import json
import anthropic
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── System prompt ─────────────────────────────────────────────────────────────
# Tells the model its role and how to think — but NOT how to format output.
# Formatting is now enforced by the tool schema below, not by words.

SYSTEM_PROMPT = """You are a lead classifier for an outdoor home services business in Fairfield County, CT.

FIRST: Determine post_type based on WHO wrote it and WHY:
- "request": A homeowner asking someone else to do work. They have a problem and need help.
- "offer": A contractor advertising their OWN services. They are selling, not buying.
- "referral": Someone recommending or reviewing a business.
- "other": Unrelated to home services entirely.

ONLY score "request" posts. Set score=1 for offer/referral/other.

Business ad signals (post_type = "offer"):
- "We offer..." / "Our company..." / "Call us" / "Book now" / "Free estimates"
- "Licensed and insured" / "X years experience" / "Serving CT homeowners"
- Website URLs or phone numbers prominently displayed

Homeowner request signals (post_type = "request"):
- "I need..." / "Looking for..." / "Can anyone recommend..."
- Personal possessives: "my chimney", "our backyard", "my driveway"
- Describing a specific problem they personally have
- "ASAP" / "urgent" / "need this done soon"

Scoring for "request" posts only:
- 9-10: Clear request with specifics (size, timeline, material, urgency word like ASAP)
- 8: Direct request for one of our services, even if brief ("need to do a walkway", "looking for someone to build a patio")
- 6-7: Likely relevant but ambiguous — could be DIY, asking for advice, or unclear scope
- 4-5: Possibly related but unclear intent
- 1-3: Not relevant

Key rule: If the post names one of our services AND uses request language ("need", "looking for", "can anyone", "who does"), score it 8 minimum. Brevity alone is NOT a reason to score below 8.

Our services: chimney repair, masonry, brickwork, tuckpointing, retaining wall, patio,
walkway, concrete, driveway, junk removal, yard cleanup, tree removal, tree trimming,
painting, power washing, landscaping, lawn care, mulching, grading, drainage."""


# ── Tool schema ───────────────────────────────────────────────────────────────
# This is the enforced structure. The API guarantees the model MUST call this
# tool and MUST return data matching every field and type defined here.
# Think of it as a contract between you and the model.

CLASSIFY_TOOL = {
    "name": "classify_lead",
    "description": "Classify a social media post as a home service lead.",
    "input_schema": {
        "type": "object",
        "properties": {
            "post_type": {
                "type": "string",
                "enum": ["request", "offer", "referral", "other"],
                "description": "Who wrote this and why"
            },
            "score": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Lead quality score. Only meaningful for request posts."
            },
            "work_type": {
                "type": "string",
                "description": "Primary service category (e.g. chimney repair, patio, tree removal)"
            },
            "urgency": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "How urgently the homeowner needs this done"
            },
            "summary": {
                "type": "string",
                "description": "One sentence summary of what they need, max 120 characters"
            }
        },
        "required": ["post_type", "score", "work_type", "urgency", "summary"]
    }
}


# ── Classifier function ───────────────────────────────────────────────────────

def classify(post_text: str, town: str = "Unknown") -> dict | None:
    """
    Classify a post using enforced tool use schema.
    Returns a dict with guaranteed fields, or None on API failure.

    Key difference from old approach:
    - Old: asked model to return JSON, parsed the text response
    - New: model MUST call classify_lead tool, API enforces the schema
    """
    if not post_text or len(post_text.strip()) < 10:
        return None

    user_message = f"Post text: {post_text[:1500]}\nTown/area: {town}\n\nClassify this post."

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            tools=[CLASSIFY_TOOL],
            # tool_choice forces the model to call our tool — it cannot respond with text
            tool_choice={"type": "tool", "name": "classify_lead"},
            messages=[{"role": "user", "content": user_message}]
        )

        # The response is always a tool_use block — no text parsing needed
        for block in response.content:
            if block.type == "tool_use" and block.name == "classify_lead":
                return block.input  # already a clean Python dict, guaranteed schema

        return None

    except Exception as e:
        print(f"[classify] API error: {e}")
        return None


# ── Test runner ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    samples = [
        ("My chimney has crumbling mortar and bricks are loose. Need someone ASAP.", "Westport"),
        ("Anyone know a good tree removal company? Large oak leaning toward the house.", "Greenwich"),
        ("Looking for someone to build a new bluestone patio, around 400 sqft.", "Darien"),
        ("Does anyone have a good pizza place recommendation in town?", "Norwalk"),
        ("We offer professional masonry services across Fairfield County. Call us!", "Stamford"),
        ("Need junk removed from my garage and basement, lots of old furniture.", "Bridgeport"),
    ]

    for text, town in samples:
        result = classify(text, town)
        if result:
            score    = result.get("score", 0)
            ptype    = result.get("post_type")
            flag     = "LEAD" if score >= 7 else "skip"
            print(f"[{flag}] {score}/10 | {ptype} | {result.get('work_type')} | {result.get('urgency')} urgency")
            print(f"       {result.get('summary')}")
        else:
            print("[ERROR] Classification failed")
        print()
