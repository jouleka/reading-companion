"""Static log-policy and content-free audit vocabulary gates for LIT-49."""

from __future__ import annotations

import ast
from pathlib import Path

from app.hosted.audit import APPROVED_SECURITY_LOG_MESSAGES

APP = Path(__file__).parents[2] / "app"


def test_hosted_application_logs_use_only_reviewed_static_messages() -> None:
    files = [APP / "main.py", *(APP / "hosted").rglob("*.py")]
    messages = set()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_LOG"
                and node.func.attr in {"debug", "info", "warning", "error", "critical", "exception"}
            ):
                continue
            assert node.args and isinstance(node.args[0], ast.Constant)
            assert isinstance(node.args[0].value, str)
            message = node.args[0].value
            messages.add(message)
            assert message in APPROVED_SECURITY_LOG_MESSAGES
            assert node.func.attr != "exception"
            assert not {keyword.arg for keyword in node.keywords} & {
                "exc_info",
                "stack_info",
                "extra",
            }
            for argument in node.args[1:]:
                assert (
                    isinstance(argument, ast.Subscript)
                    and isinstance(argument.value, ast.Name)
                    and argument.value.id == "stats"
                    and isinstance(argument.slice, ast.Constant)
                    and argument.slice.value == "active_lock_leases"
                )
    assert messages == set(APPROVED_SECURITY_LOG_MESSAGES)
