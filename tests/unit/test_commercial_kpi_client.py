"""No generic Sales entrypoint or unrelated HTTP capabilities are exposed."""

import subprocess
from pathlib import Path

import pytest

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


@pytest.mark.parametrize(
    "status,restarts",
    [(400, 3), (409, 3), (401, 1), (403, 1), (422, 1), (429, 1), (503, 1)],
)
def test_cursor_recovery_is_bounded_and_never_retries_auth_or_provider_errors(
    status, restarts
):
    from datetime import date

    from tests.integration.api.test_commercial_kpi_model import load_client

    module = load_client()
    starts, reads = [], []

    class HTTP:
        def call(self, method, path, payload=None):
            if method == "POST":
                assert payload["operation"] == "start"
                starts.append(payload)
                return {
                    "next_cursor": "synthetic-opaque",
                    "checkpoint_version": "0" * 64,
                }
            reads.append(path)
            raise RuntimeError("CRM HTTP " + str(status))

    transport = module.KPITransport.__new__(module.KPITransport)
    transport.client = HTTP()
    with pytest.raises(RuntimeError):
        module.run_reconciliation(
            transport,
            entrypoint="sales_13h",
            classification_only=True,
            anchor_date=date(2026, 9, 15),
        )
    assert len(starts) == len(reads) == restarts
    assert len({body["run_id"] for body in starts}) == 1
