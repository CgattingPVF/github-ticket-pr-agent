import json

from core import _format_codex_json_line
from workflow import WorkflowRunner


class Settings:
    claude_command = "claude -p"
    agent_command = "codex exec -"
    review_command = "codex review -"


def test_provider_commands_support_codex_claude_and_custom():
    runner = WorkflowRunner(Settings(), object())
    assert runner._provider_command("claude", "ignored", "agent") == "claude -p"
    assert runner._provider_command("codex", "ignored", "agent") == "codex exec -"
    assert runner._provider_command("codex", "ignored", "review") == "codex review -"
    assert runner._provider_command("custom", "my-agent -p", "agent") == "my-agent -p"


def test_codex_json_events_are_formatted_for_the_live_integrity_feed():
    event = {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "bun test focused.test.ts",
            "aggregated_output": "2 pass\n0 fail\n",
            "exit_code": 0,
        },
    }

    assert _format_codex_json_line(json.dumps(event)) == "2 pass\n0 fail\n[PASS · exit 0]"


def test_codex_agent_message_is_displayed_without_json_noise():
    event = {"type": "item.completed", "item": {"type": "agent_message", "text": "Inspecting the report template."}}
    assert _format_codex_json_line(json.dumps(event)) == "Inspecting the report template."
