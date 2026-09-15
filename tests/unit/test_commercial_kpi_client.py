"""No generic Sales entrypoint or unrelated HTTP capabilities are exposed."""

import subprocess
from pathlib import Path

CLIENT = (
    Path(__file__).resolve().parents[2] / "ops/hourly-sales/commercial_kpi_reconcile.py"
)
RUNTIME = "/Users/max/.hermes/hermes-agent/venv/bin/python"


def test_cli_documents_both_entrypoints_and_refuses_non_classification_mode():
    help_run = subprocess.run(
        [RUNTIME, str(CLIENT), "--help"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert help_run.returncode == 0
    assert (
        "--classification-only" in help_run.stdout
        and "sales_1730" in help_run.stdout
        and "sales_13h" in help_run.stdout
    )
    rejected = subprocess.run(
        [RUNTIME, str(CLIENT), "--entrypoint", "sales_13h"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert (
        rejected.returncode == 2 and "requires --classification-only" in rejected.stderr
    )
