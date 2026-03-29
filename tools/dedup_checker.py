"""
dedup_checker.py
Uses Claude to compare two posts and decide if they're the same request reposted.

Exercise: Boolean fields, arrays in schemas, AI judgment for cases rules can't handle.

Why AI and not rules:
    "Need chimney fixed in Westport" vs "My chimney is falling apart, need a mason"
    Same job. Different words. No keyword rule catches this — Claude can.
"""

import os
import anthropic
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── System prompt ─────────────────────────────────────────────────────────────
# Focus: thinking only. No mention of format — that's the tool's job.

SYSTEM_PROMPT = """You are a duplicate detection assistant for a lead monitoring system.

You compare two social media posts and decide if they represent the same
homeowner request reposted, or two genuinely different leads.

Same request signals:
- Same town + same service type + similar description
- One post is clearly a follow-up ("still looking", "reposting", "no responses yet")
- Posted within 48 hours of each other with nearly identical needs

Different request signals:
- Different towns (even same service = different lead)
- Same service but clearly different homeowners
- Similar topic but different specific jobs

Red flag signals (capture these):
- Post already has multiple replies but homeowner reposted anyway
- Vague request with no real details
- Posted multiple times in a short window (spammy behavior)
- Asking for free work or unrealistic pricing

Be conservative — if genuinely uncertain, recommend alert (better to alert twice
than miss a real lead)."""


# ── Tool schema ───────────────────────────────────────────────────────────────
# New concepts here:
# 1. Boolean fields — true/false values
# 2. Array of strings — a list where each item is a string
# 3. enum with 2 values — readable alternative to boolean for actions

DEDUP_TOOL = {
    "name": "check_duplicate",
    "description": "Compare two posts and decide if the second is a duplicate.",
    "input_schema": {
        "type": "object",
        "properties": {

            # Boolean — yes/no is this the same job
            "same_request": {
                "type": "boolean",
                "description": "True if both posts are asking for the same job"
            },

            # Boolean — yes/no is this the same person
            "same_user": {
                "type": "boolean",
                "description": "True if both posts appear to be from the same person"
            },

            # Enum — how sure is Claude about this decision
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "How confident is this duplicate assessment"
            },

            # String — plain english explanation we can read in logs
            "reason": {
                "type": "string",
                "description": "One sentence explaining the duplicate decision"
            },

            # Array of strings — NEW concept
            # Instead of a count (integer), we get actual useful flag descriptions
            # Claude can return 0, 1, or many flags
            "red_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of concern signals found. Empty array if none."
            },

            # Enum with 2 values — more readable than boolean true/false
            # "alert" = tell the contractor, "skip" = it's a duplicate, ignore it
            "recommendation": {
                "type": "string",
                "enum": ["alert", "skip"],
                "description": "alert = new lead worth sending, skip = duplicate"
            }
        },
        "required": [
            "same_request", "same_user", "confidence",
            "reason", "red_flags", "recommendation"
        ]
    }
}


# ── Main function ─────────────────────────────────────────────────────────────

def check_duplicate(post_a: dict, post_b: dict) -> dict | None:
    """
    Compare two posts. Returns duplicate assessment dict or None on failure.

    post_a = the existing post already in the database
    post_b = the new incoming post we're deciding whether to alert on
    """

    # Build plain text comparison for Claude to reason about
    # Notice: we describe what each post IS, not what format to return
    user_message = f"""Existing post (already alerted):
Text: {post_a.get('text', '')}
Town: {post_a.get('town', 'unknown')}
Posted: {post_a.get('posted', 'unknown')}
URL: {post_a.get('url', '')}

New incoming post (deciding whether to alert):
Text: {post_b.get('text', '')}
Town: {post_b.get('town', 'unknown')}
Posted: {post_b.get('posted', 'unknown')}
URL: {post_b.get('url', '')}

Are these the same request? Should we alert on the new post or skip it?"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            tools=[DEDUP_TOOL],
            tool_choice={"type": "tool", "name": "check_duplicate"},
            messages=[{"role": "user", "content": user_message}]
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "check_duplicate":
                return block.input

        return None

    except Exception as e:
        print(f"[dedup] API error: {e}")
        return None


# ── Test runner ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_cases = [
        # Case 1: Clear duplicate — same person reposting
        {
            "label": "Same person reposting",
            "post_a": {
                "text": "Need someone to fix my chimney, mortar is crumbling. Westport area.",
                "town": "Westport",
                "posted": "3 hours ago",
                "url": "nextdoor.com/p/abc123"
            },
            "post_b": {
                "text": "Still looking for a mason for my chimney in Westport, no responses yet",
                "town": "Westport",
                "posted": "30 minutes ago",
                "url": "nextdoor.com/p/xyz789"
            }
        },
        # Case 2: Different people — same service, different town
        {
            "label": "Different people, different towns",
            "post_a": {
                "text": "Looking for a patio builder in Norwalk, bluestone preferred",
                "town": "Norwalk",
                "posted": "5 hours ago",
                "url": "nextdoor.com/p/def456"
            },
            "post_b": {
                "text": "Anyone recommend a good patio contractor? Need work done in Bridgeport",
                "town": "Bridgeport",
                "posted": "1 hour ago",
                "url": "nextdoor.com/p/ghi789"
            }
        },
        # Case 3: Red flags — already has replies but reposted
        {
            "label": "Repost with red flags",
            "post_a": {
                "text": "Need tree removed from my backyard urgently",
                "town": "Greenwich",
                "posted": "2 hours ago",
                "url": "nextdoor.com/p/jkl012"
            },
            "post_b": {
                "text": "REPOSTING - need tree removed, had 5 contractors quote but all too expensive, need someone cheap",
                "town": "Greenwich",
                "posted": "10 minutes ago",
                "url": "nextdoor.com/p/mno345"
            }
        },
    ]

    for case in test_cases:
        print(f"Test: {case['label']}")
        print("-" * 40)
        result = check_duplicate(case["post_a"], case["post_b"])
        if result:
            print(f"Same request:     {result['same_request']}")
            print(f"Same user:        {result['same_user']}")
            print(f"Confidence:       {result['confidence']}")
            print(f"Recommendation:   {result['recommendation'].upper()}")
            print(f"Reason:           {result['reason']}")
            # Array — loop through each flag
            if result["red_flags"]:
                print(f"Red flags:")
                for flag in result["red_flags"]:
                    print(f"  - {flag}")
            else:
                print(f"Red flags:        none")
        else:
            print("ERROR: check failed")
        print()
