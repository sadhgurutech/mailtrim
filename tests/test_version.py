"""Tests for --version flag and version command."""

from __future__ import annotations

from typer.testing import CliRunner

from mailtrim import __version__
from mailtrim.cli.main import app

runner = CliRunner()


def test_version_flag_exits_zero():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0


def test_version_flag_output():
    result = runner.invoke(app, ["--version"])
    assert result.output.strip() == f"mailtrim {__version__}"


def test_version_flag_short_form():
    result = runner.invoke(app, ["-V"])
    assert result.output.strip() == f"mailtrim {__version__}"


def test_version_command_exits_zero():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0


def test_version_command_output():
    result = runner.invoke(app, ["version"])
    assert result.output.strip() == f"mailtrim {__version__}"


def test_version_contains_package_version():
    result = runner.invoke(app, ["--version"])
    assert __version__ in result.output


def test_version_flag_and_command_match():
    flag_out = runner.invoke(app, ["--version"]).output.strip()
    cmd_out = runner.invoke(app, ["version"]).output.strip()
    assert flag_out == cmd_out
