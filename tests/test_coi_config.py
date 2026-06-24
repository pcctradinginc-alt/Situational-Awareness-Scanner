"""Tests for config loading + graceful defaults."""
from pathlib import Path

from call_options_intel.config_loader import AppConfig, load_yaml


def test_loads_real_configs():
    cfg = AppConfig()
    assert cfg.scoring["weights"]["thesis"] > 0
    assert cfg.universe["tickers"], "universe should have tickers"
    assert cfg.mode in ("offline", "live")


def test_missing_dir_falls_back_to_defaults(tmp_path):
    cfg = AppConfig(config_dir=tmp_path)  # empty dir -> all defaults
    assert cfg.scoring["weights"]["thesis"] == 0.28
    assert cfg.risk["dte"]["min"] == 30
    # universe default is empty -> validate warns
    warnings = cfg.validate()
    assert any("universe" in w for w in warnings)


def test_deep_merge_preserves_unspecified_keys(tmp_path):
    p = tmp_path / "scoring_weights.yml"
    p.write_text("weights:\n  thesis: 0.5\n")
    merged = load_yaml("scoring_weights", tmp_path)
    assert merged["weights"]["thesis"] == 0.5          # overridden
    assert merged["weights"]["market"] == 0.22         # default preserved
    assert "risk_penalty" in merged                    # whole default block kept


def test_validate_flags_bad_dte(tmp_path):
    (tmp_path / "risk_thresholds.yml").write_text("dte:\n  min: 100\n  max: 50\n")
    cfg = AppConfig(config_dir=tmp_path)
    assert any("dte" in w for w in cfg.validate())
