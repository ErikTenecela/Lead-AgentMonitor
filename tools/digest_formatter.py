"""
digest_formatter.py
Takes overnight leads from the database and uses Claude to format
a single smart morning digest message for Telegram.

Exercise: Nested schemas with forced tool use.
"""

import os
import json
import sqlite3
import anthropic
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

client   = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
DB_PATH  = Path(__file__).parent.parent / ".tmp" / "posts.db"
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "7"))


# ── System prompt ─────────────────────────────────────────────────────────────
# Notice: we say nothing about JSON format here.
# Format is the tool's job — the system prompt focuses purely on thinking.

SYSTEM_PROMPT = """You are a morning briefing assistant for an outdoor home services
contractor in Fairfield County, CT.

You receive overnight leads that came in while the contractor was sleeping.
Your job:
- Identify the single best lead to act on first (highest score + highest urgency)
- Write a short, clear digest_message the contractor reads on his phone at 7am
- Be direct and practical — he's deciding who to call first

The digest_message should read naturally, like a text from a smart assistant.
Example: "3 leads overnight. Top pick: chimney repair in Westport (9/10, urgent).
Also worth checking: tree removal Bridgeport + patio Norwalk."
"""


# ── Tool schema ───────────────────────────────────────────────────────────────
# This is the nested schema you designed.
# "top_lead" is an object INSIDE the main object — that's what nested means.
# The API enforces every field, every type, every enum value.

DIGEST_TOOL = {
    "name": "format_digest",
    "description": "Format overnight leads into a morning briefing.",
    "input_schema": {
        "type": "object",
        "properties": {

            # Your "number" field — how many leads total
            "total_leads": {
                "type": "integer",
                "description": "Total number of qualifying overnight leads"
            },

            # Nested object — a schema inside a schema
            # This is what makes it "nested"
            "top_lead": {
                "type": "object",
                "description": "The single highest priority lead to act on first",
                "properties": {

                    # Your "text" fields
                    "summary": {
                        "type": "string",
                        "description": "What the homeowner needs"
                    },
                    "town": {
                        "type": "string",
                        "description": "City where the job is"
                    },
                    "url": {
                        "type": "string",
                        "description": "Link to the original post"
                    },

                    # Your "number" fields
                    "score": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10
                    },
                    "hours_old": {
                        "type": "integer",
                        "description": "How many hours ago this was posted"
                    },

                    # Your "only three options" field — enum in action
                    "urgency": {
                        "type": "string",
                        "enum": ["high", "medium", "low"]
                    }
                },
                "required": ["summary", "score", "town", "urgency", "url", "hours_old"]
            },

            # Your "text" field — the pre-written Telegram message
            "digest_message": {
                "type": "string",
                "description": "Natural language morning briefing, ready to send"
            }

        },
        "required": ["total_leads", "top_lead", "digest_message"]
    }
}


# ── Main function ─────────────────────────────────────────────────────────────

def format_digest(leads: list[dict]) -> dict | None:
    """
    Takes a list of overnight lead dicts, returns a formatted digest.
    Each lead dict needs: score, work_type, town, urgency, url, summary, age_minutes
    """
    if not leads:
        return None

    # Build a plain text summary of leads to pass to Claude
    # Notice: we're not asking Claude to format JSON — that's the tool's job
    # We're asking it to THINK about the leads
    leads_text = ""
    for i, lead in enumerate(leads, 1):
        hours = round((lead.get("age_minutes") or 0) / 60, 1)
        leads_text += (
            f"Lead {i}: {lead.get('work_type', 'unknown')} in {lead.get('town', 'unknown')}\n"
            f"  Score: {lead.get('score')}/10 | Urgency: {lead.get('urgency', 'unknown')}\n"
            f"  Summary: {lead.get('summary', '')}\n"
            f"  Posted: {hours} hours ago\n"
            f"  URL: {lead.get('url', '')}\n\n"
        )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            tools=[DIGEST_TOOL],
            # Force the model to call format_digest — no escape to plain text
            tool_choice={"type": "tool", "name": "format_digest"},
            messages=[{
                "role": "user",
                "content": f"Here are the overnight leads:\n\n{leads_text}\nFormat the morning digest."
            }]
        )

        # No JSON parsing, no regex — just grab the tool input directly
        for block in response.content:
            if block.type == "tool_use" and block.name == "format_digest":
                return block.input  # guaranteed to match schema

        return None

    except Exception as e:
        print(f"[digest] API error: {e}")
        return None


# ── Test runner ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Simulate overnight leads
    test_leads = [
        {
            "score": 9,
            "work_type": "chimney repair",
            "town": "Westport",
            "urgency": "high",
            "summary": "Crumbling mortar, bricks loose, needs repair ASAP before winter",
            "url": "https://nextdoor.com/p/abc123",
            "age_minutes": 480  # 8 hours ago
        },
        {
            "score": 7,
            "work_type": "patio",
            "town": "Norwalk",
            "urgency": "medium",
            "summary": "Looking for bluestone patio builder, approx 400 sqft backyard",
            "url": "https://nextdoor.com/p/def456",
            "age_minutes": 360  # 6 hours ago
        },
        {
            "score": 8,
            "work_type": "tree removal",
            "town": "Bridgeport",
            "urgency": "high",
            "summary": "Large oak leaning toward house after storm, needs removal",
            "url": "https://nextdoor.com/p/ghi789",
            "age_minutes": 240  # 4 hours ago
        },
    ]

    print("Running digest formatter...\n")
    result = format_digest(test_leads)

    if result:
        print(f"Total leads: {result['total_leads']}")
        print(f"Digest message:\n  {result['digest_message']}")
        print(f"\nTop lead:")
        top = result["top_lead"]
        print(f"  {top['score']}/10 | {top['urgency'].upper()} | {top['town']}")
        print(f"  {top['summary']}")
        print(f"  Posted {top['hours_old']}h ago")
        print(f"  {top['url']}")
    else:
        print("Digest failed")
