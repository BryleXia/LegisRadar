"""十、配置。"""

from __future__ import annotations

import pathlib
import re
import tomllib

from conftest import ROOT, item, run_cli, write_config, write_index
from legisradar import config

SHIPPED = ROOT / "config.toml"


def test_double_quoted_windows_path_is_recorded_as_unreadable(root):
    write_config(root, '[general]\ndata_dir = "D:\\viewer"\n')
    cfg = config.load(root)
    assert config.CONFIG_ERROR is not None
    assert "config.toml" in config.CONFIG_ERROR
    assert "'" in config.CONFIG_ERROR or "单引号" in config.CONFIG_ERROR
    assert cfg.error == config.CONFIG_ERROR


def test_single_quoted_windows_path_keeps_its_backslashes(root):
    write_config(root, "[general]\ndata_dir = 'E:\\legisradar-data'\n")
    cfg = config.load(root)
    assert config.CONFIG_ERROR is None
    assert cfg.data_dir == pathlib.Path("E:/legisradar-data")
    assert "legisradar-data" in str(cfg.data_dir)
    assert cfg.data_dir_raw == "E:\\legisradar-data"


def test_a_healthy_config_records_no_error(root):
    cfg = config.load(root)
    assert cfg.error is None
    assert config.CONFIG_ERROR is None
    assert cfg.data_dir == root / "data"
    assert cfg.board == root / "board.html"
    assert cfg.port == 8765


def test_broken_config_stops_the_writing_commands(root):
    good = write_index(root / "in.json", [item()])
    write_config(root, '[general]\ndata_dir = "D:\\viewer"\n')
    code, out, err = run_cli(["sync", "--from", str(good)], root)
    assert code == 1
    assert "config.toml" in out
    assert "先把 config.toml 修好再同步" in out
    assert not (root / "data").exists()


def test_read_only_commands_run_with_defaults_under_a_broken_config(root):
    write_config(root, '[general]\ndata_dir = "D:\\viewer"\n')
    assert run_cli(["status"], root)[0] == 0
    assert run_cli(["board"], root)[0] == 0


def test_relative_paths_resolve_against_the_program_root(root):
    write_config(root, '[general]\ndata_dir = "store"\nboard = "out/board.html"\n')
    cfg = config.load(root)
    assert cfg.data_dir == root / "store"
    assert cfg.board == root / "out" / "board.html"
    assert cfg.data_dir_raw == "store"


def test_root_can_be_pointed_elsewhere(root, tmp_path, monkeypatch):
    other = tmp_path / "别处"
    other.mkdir()
    monkeypatch.setenv(config.ROOT_ENV, str(other))
    assert config.program_root() == other.resolve()


# ------------------------------------------------------------ 出厂配置


def shipped() -> dict:
    return tomllib.loads(SHIPPED.read_text(encoding="utf-8"))


def test_shipped_index_addresses_are_not_empty():
    viewer = shipped()["viewer"]
    assert viewer["index_url"]
    assert viewer["index_mirrors"]
    assert all(url for url in viewer["index_mirrors"])


def test_shipped_addresses_point_at_the_same_file():
    viewer = shipped()["viewer"]
    addresses = [viewer["index_url"], *viewer["index_mirrors"]]
    targets = set()
    for url in addresses:
        jsdelivr = re.match(r"https://([\w.\-]+)/gh/([^/]+)/([^/@]+)@([^/]+)/(.+)$", url)
        raw = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$", url)
        assert jsdelivr or raw, url
        if jsdelivr:
            _, owner, repo, _ref, path = jsdelivr.groups()
        else:
            owner, repo, _ref, path = raw.groups()
        targets.add((owner.lower(), repo.lower(), path))
    assert len(targets) == 1
    assert targets.pop()[2] == "index.json"


def test_shipped_jsdelivr_addresses_carry_the_gh_prefix():
    viewer = shipped()["viewer"]
    for url in [viewer["index_url"], *viewer["index_mirrors"]]:
        if "jsdelivr.net" in url:
            assert "/gh/" in url


def test_at_least_one_mirror_is_not_raw_githubusercontent():
    mirrors = shipped()["viewer"]["index_mirrors"]
    assert mirrors
    assert any("raw.githubusercontent.com" not in url for url in mirrors)


def test_primary_is_not_raw_githubusercontent():
    assert "raw.githubusercontent.com" not in shipped()["viewer"]["index_url"]


def test_shipped_config_is_readable():
    cfg = config.load(ROOT)
    assert cfg.error is None
    assert cfg.index_url
    assert cfg.index_mirrors
