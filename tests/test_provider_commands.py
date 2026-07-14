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
