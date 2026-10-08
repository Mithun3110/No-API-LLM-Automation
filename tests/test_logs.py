"""Run folders and the masked logger: nothing sensitive reaches disk."""

from decimal import Decimal

from src.logs import RunFolder, RunLogger, new_run_id, read_log
from src.models import ControlState, FailureInfo, RunResult, RunStatus
from src.safety import Masker, load_policy


def make_logger(tmp_path) -> RunLogger:
    masker = Masker(load_policy().mask_fields, secrets=["demo123"])
    return RunLogger(RunFolder(runs_dir=tmp_path), masker, echo=False)


def test_log_lines_are_masked_and_typed(tmp_path):
    logger = make_logger(tmp_path)
    logger.log("replay", "ok", ControlState.AUTOMATION, step=2, action="type", target='role=textbox "Member ID"',
               reason="Typed 12345", data={"value": "12345", "member_id": "12345"})
    logger.log("session", "ok", ControlState.AUTOMATION, action="login", reason="password demo123 accepted")
    raw = logger.folder.log_path.read_text()
    assert "12345" not in raw and "demo123" not in raw
    entries = read_log(logger.folder.log_path)
    assert entries[0].reason == "Typed ***45" and entries[0].data == {"value": "***45", "member_id": "***45"}
    assert entries[1].controller == ControlState.AUTOMATION


def test_result_file_masks_outputs_but_keeps_structure(tmp_path):
    logger = make_logger(tmp_path)
    result = RunResult(status=RunStatus.SUCCESS, run_id=logger.run_id, mode="replay",
                       recipe_id="member.lookup_savings_balance", recipe_version="1.0.0",
                       outputs={"savings_balance": Decimal("16057.78")}, log_file=str(logger.folder.log_path))
    text = logger.write_result(result).read_text()
    assert "16057" not in text and '"savings_balance": "***"' in text
    assert logger.run_id in text and "member.lookup_savings_balance" in text
    assert result.outputs["savings_balance"] == Decimal("16057.78")  # caller still has the real value


def test_failure_evidence_paths_stay_exact(tmp_path):
    logger = make_logger(tmp_path)
    shot = str(logger.folder.screenshot_path("failure"))
    result = RunResult(status=RunStatus.FAILED, run_id=logger.run_id, mode="replay",
                       failure=FailureInfo(step=3, expected="Member Detail", observed="Something went wrong. ref 12345",
                                           error_type="server_error", evidence=[shot]))
    text = logger.write_result(result).read_text()
    assert shot in text and "ref ***45" in text


def test_run_ids_survive_masking():
    run_id = new_run_id()
    masker = Masker(load_policy().mask_fields)
    assert masker.mask_text(f"runs/{run_id}/01_failure.png") == f"runs/{run_id}/01_failure.png"


def test_each_run_gets_its_own_folder(tmp_path):
    a, b = RunFolder(runs_dir=tmp_path), RunFolder(runs_dir=tmp_path)
    assert a.path != b.path and a.path.is_dir()
    assert a.screenshot_path("x").name == "01_x.png" and a.screenshot_path("y").name == "02_y.png"
