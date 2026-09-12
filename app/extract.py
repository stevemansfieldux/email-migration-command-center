"""Turn a meeting transcript or a Slack digest into candidate tasks.

Deliberately conservative: it is better to miss a task than to fill the board with
things nobody agreed to. Every task carries the quote it came from so a human can
check it in one glance.
"""
import json
import os
from typing import Any

SYSTEM = """You extract action items from a conversation between two business partners.

Return ONLY tasks that someone actually committed to, or that the conversation clearly
left as a required next step. Do not invent work, do not turn opinions into tasks, and
do not create a task for something already described as done.

For each task give:
  title      an imperative phrase under 80 characters, starting with a verb
  detail     one or two sentences of context, enough to act on without the transcript
  owner      the person who took it on, or "unassigned" if genuinely unclear
  priority   low | normal | high
  due        YYYY-MM-DD if a date was actually stated, otherwise null
  quote      the shortest verbatim span from the source that supports this task

If nothing was committed to, return an empty list. That is a valid and common answer."""


class NotConfigured(RuntimeError):
    """No usable credential. The SDK looks for ANTHROPIC_API_KEY, then
    ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile — so this is raised
    only when none of those resolved, not merely when the env var is unset."""


def extract(text: str, participants: list[str] | None = None) -> list[dict[str, Any]]:
    import anthropic

    who = ", ".join(participants) if participants else "the participants"
    client = anthropic.Anthropic()
    try:
        resp = _call(client, who, text)
    except anthropic.AuthenticationError as e:
        raise NotConfigured("The API rejected the credential — re-run `ant auth login` or check the key") from e
    except TypeError as e:
        from .auth_status import is_no_credential
        if not is_no_credential(e):
            raise
        raise NotConfigured("No Anthropic credential resolved — run `ant auth login` or set ANTHROPIC_API_KEY") from e
    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_tasks":
            return block.input.get("tasks", [])
    return []


def _call(client, who: str, text: str):
    return client.messages.create(
        model=os.environ.get("EXTRACT_MODEL", "claude-opus-5"),
        max_tokens=4000,
        system=SYSTEM,
        tools=[{
            "name": "record_tasks",
            "description": "Record the action items found in the source.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "detail": {"type": "string"},
                                "owner": {"type": "string"},
                                "priority": {"enum": ["low", "normal", "high"]},
                                "due": {"type": ["string", "null"]},
                                "quote": {"type": "string"},
                            },
                            "required": ["title", "detail", "owner", "priority", "quote"],
                        },
                    }
                },
                "required": ["tasks"],
            },
        }],
        tool_choice={"type": "tool", "name": "record_tasks"},
        messages=[{
            "role": "user",
            "content": f"Participants: {who}\n\nSource:\n\n{text}",
        }],
    )


def to_json(tasks: list[dict[str, Any]]) -> str:
    return json.dumps(tasks, indent=2)
