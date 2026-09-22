import asyncio
import logging

from app.models import ChatCompletionRequest, ChatMessage
from mock_llm.main import BACKEND_ID, chat_completions, inspect_messages


def test_safe_backend_diagnostic_log(caplog, monkeypatch) -> None:
    monkeypatch.delenv("MOCK_LOG_REQUEST_BODY", raising=False)
    raw_email = "private.user@example.org"
    raw_phone = "+7 999 111-22-33"
    body = ChatCompletionRequest(
        model="mock-model",
        messages=[
            ChatMessage(
                role="user",
                content="Получены __PII_EMAIL_1__ и __PII_PHONE_1__",
            )
        ],
        stream=False,
    )

    with caplog.at_level(logging.INFO, logger="privygate.mock_llm"):
        asyncio.run(chat_completions(body))

    log_output = caplog.text
    assert f"backend_id={BACKEND_ID}" in log_output
    assert "pii_placeholder_count=2" in log_output
    assert "raw_email_detected=false" in log_output
    assert "raw_phone_detected=false" in log_output
    assert raw_email not in log_output
    assert raw_phone not in log_output


def test_diagnostics_detect_raw_pii_without_exposing_values(
    caplog,
    monkeypatch,
) -> None:
    monkeypatch.delenv("MOCK_LOG_REQUEST_BODY", raising=False)
    raw_email = "private.user@example.org"
    raw_phone = "+7 999 111-22-33"
    content = f"Контакты: {raw_email}, {raw_phone}"
    diagnostics = inspect_messages([content])
    body = ChatCompletionRequest(
        model="mock-model",
        messages=[ChatMessage(role="user", content=content)],
        stream=False,
    )

    with caplog.at_level(logging.INFO, logger="privygate.mock_llm"):
        asyncio.run(chat_completions(body))

    assert diagnostics.pii_placeholder_count == 0
    assert diagnostics.raw_email_detected is True
    assert diagnostics.raw_phone_detected is True
    assert "raw_email_detected=true" in caplog.text
    assert "raw_phone_detected=true" in caplog.text
    assert raw_email not in caplog.text
    assert raw_phone not in caplog.text


def test_dev_full_request_logging_is_explicitly_opt_in(caplog, monkeypatch) -> None:
    monkeypatch.setenv("MOCK_LOG_REQUEST_BODY", "true")
    content = "Получены __PII_EMAIL_1__ и незамаскированное значение"
    body = ChatCompletionRequest(
        model="mock-model",
        messages=[ChatMessage(role="user", content=content)],
        stream=True,
    )

    with caplog.at_level(logging.INFO, logger="privygate.mock_llm"):
        asyncio.run(chat_completions(body))

    assert "DEV_ONLY full_request_logging_enabled" in caplog.text
    assert '"model":"mock-model"' in caplog.text
    assert '"stream":true' in caplog.text
    assert content in caplog.text
