from src.history_refresh import run_history_refresh


def test_history_refresh_dry_run_rebuilds_combined_history(capsys) -> None:
    assert run_history_refresh(dry_run=True) is None

    output = capsys.readouterr().out
    assert "-m src.reconciliation_store" in output
    assert "--mode combine-discovered" in output
    assert "--dataset-name all_carrier_history" in output
