"""CLI tests for ``awareness digest --email`` (SMTP delivery).

Drives the command through Typer's CliRunner with a fake SMTP transport
(monkeypatched over ``smtplib.SMTP`` / ``smtplib.SMTP_SSL``) that records
the message handed to ``send_message``, plus the failure paths (missing
server config, connection/auth failures) which must exit 1 with a clear
rich error.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from awareness.cli.main import app
from tests.unit.test_cli_digest import _corpus

runner = CliRunner()


class FakeSMTP:
    """Records every interaction; behaves like smtplib.SMTP/SMTP_SSL."""

    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host: str, port: int, timeout: int = 30) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[str] = []
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, msg: EmailMessage) -> None:
        self.calls.append("send_message")
        self.sent.append(msg)

    def quit(self) -> None:
        self.calls.append("quit")

    def close(self) -> None:
        self.calls.append("close")


class FailingSMTP(FakeSMTP):
    """Raises on the first interesting call."""

    def __init__(
        self, host: str, port: int, timeout: int = 30, *, fail_on: str = "send"
    ) -> None:
        super().__init__(host, port, timeout=timeout)
        self.fail_on = fail_on

    def send_message(self, msg: EmailMessage) -> None:
        if self.fail_on == "send":
            raise smtplib.SMTPRecipientsRefused({str(msg["To"]): (550, "no such user")})
        super().send_message(msg)

    def login(self, user: str, password: str) -> None:
        if self.fail_on == "login":
            raise smtplib.SMTPAuthenticationError(535, b"auth failed")
        super().login(user, password)


def _patch_failing(monkeypatch: pytest.MonkeyPatch, fail_on: str) -> None:
    monkeypatch.setattr(
        smtplib,
        "SMTP",
        lambda host, port, timeout=30: FailingSMTP(host, port, timeout, fail_on=fail_on),
    )


@pytest.fixture(autouse=True)
def _fake_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_PORT", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("EMAIL_FROM", raising=False)


def _only_fake(host: str = "smtp.example.com") -> FakeSMTP:
    assert len(FakeSMTP.instances) == 1, FakeSMTP.instances
    fake = FakeSMTP.instances[0]
    assert fake.host == host
    return fake


def test_digest_email_sends_plaintext_markdown(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(
        app,
        [
            "digest",
            "--email", "me@example.com",
            "--smtp-host", "smtp.example.com",
            "--smtp-port", "2525",
            "--smtp-user", "u1",
            "--smtp-password", "p1",
            "--from", "digest@example.com",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Digest emailed to me@example.com" in result.output
    assert "# Weekly Digest" not in result.output  # emailed, not printed locally

    fake = _only_fake()
    assert fake.calls == ["ehlo", "login:u1:p1", "send_message", "quit"]
    assert len(fake.sent) == 1
    msg = fake.sent[0]
    assert msg["To"] == "me@example.com"
    assert msg["From"] == "digest@example.com"
    assert msg["Subject"].startswith("Awareness digest — ")
    assert len(msg["Subject"]) == len("Awareness digest — ") + 10  # YYYY-MM-DD
    assert msg.get_content_type() == "text/plain"
    body = msg.get_content()
    assert "# Weekly Digest" in body
    assert "bitcoin" in body  # top term from the tiny corpus


def test_digest_email_uses_ssl_on_port_465(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(
        app,
        [
            "digest",
            "--email", "me@example.com",
            "--smtp-host", "smtp.example.com",
            "--smtp-port", "465",
        ],
    )
    assert result.exit_code == 0, result.output
    _only_fake()  # FakeSMTP doubles as the SSL transport


def test_digest_email_env_fallback(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _corpus(tmp_project)
    monkeypatch.setenv("SMTP_HOST", "env.example.com")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_USER", "env-user")
    monkeypatch.setenv("SMTP_PASSWORD", "env-pass")
    monkeypatch.setenv("EMAIL_FROM", "env-from@example.com")
    result = runner.invoke(app, ["digest", "--email", "me@example.com"])
    assert result.exit_code == 0, result.output

    fake = _only_fake("env.example.com")
    assert fake.port == 2525
    assert "login:env-user:env-pass" in fake.calls
    assert fake.sent[0]["From"] == "env-from@example.com"


def test_digest_email_missing_smtp_host_exits_1(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["digest", "--email", "me@example.com"])
    assert result.exit_code == 1
    assert "Email delivery needs an SMTP server" in result.output
    assert "--smtp-host" in result.output
    assert "SMTP_HOST" in result.output
    assert FakeSMTP.instances == []  # never even tried to connect


def test_digest_email_send_failure_exits_1(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _corpus(tmp_project)
    _patch_failing(monkeypatch, "send")
    result = runner.invoke(
        app,
        ["digest", "--email", "me@example.com", "--smtp-host", "smtp.example.com"],
    )
    assert result.exit_code == 1
    assert "Email delivery failed" in result.output
    assert "550" in result.output  # SMTPRecipientsRefused detail surfaced


def test_digest_email_auth_failure_exits_1(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _corpus(tmp_project)
    _patch_failing(monkeypatch, "login")
    result = runner.invoke(
        app,
        [
            "digest",
            "--email", "me@example.com",
            "--smtp-host", "smtp.example.com",
            "--smtp-user", "u",
            "--smtp-password", "bad",
        ],
    )
    assert result.exit_code == 1
    assert "Email delivery failed" in result.output
    assert "535" in result.output  # SMTPAuthenticationError detail surfaced


def test_digest_email_local_printing_kept_when_no_email(tmp_project: Path) -> None:
    _corpus(tmp_project)
    result = runner.invoke(app, ["digest", "--markdown"])
    assert result.exit_code == 0, result.output
    assert "# Weekly Digest" in result.output
    assert "bitcoin" in result.output
    assert FakeSMTP.instances == []
