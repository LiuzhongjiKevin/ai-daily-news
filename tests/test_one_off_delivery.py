import runpy
from pathlib import Path
from unittest.mock import Mock


def test_one_off_uses_fresh_state_without_changing_production(monkeypatch, tmp_path):
    main = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts" / "send_one_off_digest.py")
    )["main"]
    production = tmp_path / "data" / "state.json"
    production.parent.mkdir()
    production.write_text('{"delivery_intents": {"today": "reserved"}}')
    original = production.read_bytes()
    pipeline = Mock(output_root=tmp_path)
    pipeline.run.return_value.sent = True
    monkeypatch.setenv("MAIL_PROVIDER", "qq")
    monkeypatch.setitem(main.__globals__, "build_pipeline", lambda: pipeline)
    assert main() == 0
    assert pipeline.state_store.data_dir != production.parent
    assert production.read_bytes() == original
    options = pipeline.run.call_args.args[0]
    assert options.send and options.ai_mode == "off" and not options.force
