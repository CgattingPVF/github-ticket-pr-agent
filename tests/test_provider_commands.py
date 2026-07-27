import json
from pathlib import Path

import pytest

from core import _format_codex_json_line
from workflow import WorkflowRunner
from core import command_exists, escalate_model_command, run_command, run_configured_command


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


def test_zero_timeout_allows_command_to_finish(tmp_path):
    sleeper = tmp_path / "slow-agent"
    sleeper.write_text("#!/bin/sh\nsleep 0.05\nprintf done\n", encoding="utf-8")
    sleeper.chmod(0o755)

    result = run_command([str(sleeper)], timeout=0)

    assert result.stdout == "done"


def test_command_exists_validates_the_executable_token_in_a_full_command(tmp_path: Path):
    executable = tmp_path / "agent-bin"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)

    assert command_exists(f'{executable} exec -c \'model="gpt-5.6-luna"\' --json -')
    assert not command_exists(f'{tmp_path / "missing-bin"} exec --json -')


def test_model_escalation_promotes_codex_and_preserves_reasoning_effort():
    command = "codex exec -c 'model=\"gpt-5.6-luna\"' -c 'model_reasoning_effort=\"low\"' --json -"
    promoted, previous, current = escalate_model_command(command)
    assert (previous, current) == ("gpt-5.6-luna", "claude-opus-4-7")
    assert "model=\"claude-opus-4-7\"" in promoted
    assert "model_reasoning_effort=\"low\"" in promoted


def test_model_escalation_promotes_claude_through_each_tier():
    sonnet_command, previous, current = escalate_model_command(
        "claude -p --model claude-haiku-4-5 --verbose"
    )
    assert (previous, current) == ("claude-haiku-4-5", "claude-sonnet-5")
    assert "--model claude-sonnet-5" in sonnet_command

    luna_current = escalate_model_command(
        "codex exec -c 'model=\"claude-sonnet-5\"' --json -"
    )
    assert luna_current[1:] == ("claude-sonnet-5", "gpt-5.6-luna")


def test_model_escalation_stops_at_highest_configured_tier():
    command = "codex exec -c 'model=\"gpt-5.6-sol-high\"' --json -"
    assert escalate_model_command(command) == (command, "gpt-5.6-sol-high", None)


def test_runner_escalates_luna_to_configured_claude_opus_command():
    runner = WorkflowRunner(Settings(), object())

    command, previous, current = runner._escalate_command(
        "codex exec -c 'model=\"gpt-5.6-luna\"' -c 'model_reasoning_effort=\"low\"' --json -"
    )

    assert command == "claude -p --model claude-opus-4-7"
    assert (previous, current) == ("gpt-5.6-luna", "claude-opus-4-7")


def test_runner_escalates_claude_opus_to_configured_codex_sol_low_command():
    runner = WorkflowRunner(Settings(), object())

    command, previous, current = runner._escalate_command(
        "claude -p --model claude-opus-4-7 --verbose"
    )

    assert "model=\"gpt-5.6-sol\"" in command
    assert "model_reasoning_effort=\"low\"" in command
    assert (previous, current) == ("claude-opus-4-7", "gpt-5.6-sol")


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

    formatted = _format_codex_json_line(json.dumps(event))
    telemetry = json.loads(formatted)

    assert telemetry == {
        "mergequest_telemetry": 1,
        "kind": "command_completed",
        "command": "bun test focused.test.ts",
        "output": "2 pass\n0 fail",
        "exit_code": 0,
        "status": "done",
    }
    assert len(formatted.splitlines()) == 1


def test_codex_agent_message_is_displayed_without_json_noise():
    event = {"type": "item.completed", "item": {"type": "agent_message", "text": "Inspecting the report template."}}
    assert json.loads(_format_codex_json_line(json.dumps(event))) == {
        "mergequest_telemetry": 1,
        "kind": "agent_update",
        "message": "Inspecting the report template.",
    }
