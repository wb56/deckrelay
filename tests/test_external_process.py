import sys
from time import monotonic

from party_player.external_process import ExternalProcessRunner, diagnostic_output_excerpt


def test_runner_preserves_argument_boundaries_without_shell() -> None:
    runner = ExternalProcessRunner()

    result = runner.run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "Pfad mit Leerzeichen"]
    )

    assert result.succeeded
    assert result.stdout.strip() == "Pfad mit Leerzeichen"
    assert result.command[-1] == "Pfad mit Leerzeichen"


def test_runner_has_hard_timeout_and_returns_structured_result() -> None:
    runner = ExternalProcessRunner(default_timeout_seconds=0.1)
    started = monotonic()

    result = runner.run([sys.executable, "-c", "import time; time.sleep(5)"])

    assert result.timed_out
    assert not result.succeeded
    # Windows also waits for bounded task-tree termination after the hard timeout.
    assert monotonic() - started < 3.0


def test_runner_bounds_large_output() -> None:
    runner = ExternalProcessRunner(maximum_output_bytes=512)

    result = runner.run([sys.executable, "-c", "print('x' * 5000)"])

    assert result.return_code == 0
    assert result.output_truncated
    assert len(result.stdout.encode()) <= 512


def test_runner_reports_missing_executable_without_exception() -> None:
    result = ExternalProcessRunner().run(["definitely-missing-partyplayer-tool.exe"])

    assert not result.succeeded
    assert result.return_code is None
    assert result.error


def test_diagnostic_excerpt_keeps_only_printable_first_lines() -> None:
    output = "\nfirst\x00 line\nsecond line\nthird line\nfourth private detail\n"

    excerpt = diagnostic_output_excerpt(output, maximum_lines=2)

    assert excerpt == "first line | second line"
    assert "private" not in excerpt


def test_start_error_does_not_repeat_missing_executable_path() -> None:
    private_path = r"C:\Users\Private Person\secret-tool.exe"

    result = ExternalProcessRunner().run([private_path])

    assert private_path not in result.error
    assert "Prozess konnte nicht gestartet werden" in result.error
