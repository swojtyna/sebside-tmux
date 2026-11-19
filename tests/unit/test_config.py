import pytest

from sebside.config import Config, config_path, env_overrides, load, load_from_env, parse
from sebside.errors import ConfigError


def test_missing_file_gives_defaults(tmp_path):
    config = load(tmp_path / "nope.toml")
    assert config == Config()
    assert config.width == 36 and config.history == 5 and config.sort == "attention"
    assert config.resume_programs == ()


def test_xdg_and_override_paths():
    assert config_path({"XDG_CONFIG_HOME": "/x"}).as_posix() == "/x/sebside/config.toml"
    assert config_path({"HOME": "/home/me"}).as_posix() == "/home/me/.config/sebside/config.toml"
    assert config_path({"SEBSIDE_CONFIG": "/etc/s.toml"}).as_posix() == "/etc/s.toml"


def test_full_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
width = 42
history = 3
stale_hours = 6
quiet_minutes = 2.5
sort = "recent"
resume_programs = ["agent", "/usr/bin/vim"]

[icons]
waiting = "💬"
working = ".oO"

[colors]
waiting = "bright-magenta"
done = "#00ff88"

[filter]
include = ["*"]
exclude = ["scratch-*"]
"""
    )
    config = load(path)
    assert (config.width, config.history, config.stale_hours, config.quiet_minutes) == (42, 3, 6.0, 2.5)
    assert config.sort == "recent"
    assert config.icons["waiting"] == "💬" and config.icons["error"] == "✖"
    assert config.icon_frames("working") == ".oO"
    assert config.colors["waiting"] == "bright-magenta" and config.colors["done"] == "#00ff88"
    assert config.colors["error"] == "red"
    assert not config.filter.allows("scratch-1") and config.filter.allows("main")
    assert config.may_resume(("agent", "--resume", "x"))
    assert config.may_resume(("/usr/bin/vim",))
    assert not config.may_resume(("vim",))
    assert not config.may_resume(("/tmp/agent",))
    assert not config.may_resume(())


def test_precedence_toml_then_env_then_flags(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('width = 30\nhistory = 2\nsort = "name"\n')
    env = {"SEBSIDE_CONFIG": str(path), "SEBSIDE_WIDTH": "40", "SEBSIDE_HISTORY": "4"}
    config = load_from_env(env, {"history": 9})
    assert (config.width, config.history, config.sort) == (40, 9, "name")


def test_bad_env_value():
    with pytest.raises(ConfigError):
        env_overrides({"SEBSIDE_WIDTH": "wide"})


@pytest.mark.parametrize(
    "data",
    [
        {"widht": 30},
        {"width": "wide"},
        {"width": 5},
        {"history": True},
        {"sort": "random"},
        {"icons": {"sleeping": "z"}},
        {"icons": {"waiting": ""}},
        {"colors": {"waiting": "orange"}},
        {"colors": {"waiting": "#fff"}},
        {"filter": {"exclude": "scratch-*"}},
        {"resume_programs": "agent"},
    ],
)
def test_bad_values_are_errors(data):
    with pytest.raises(ConfigError):
        parse(data)


def test_syntax_error_names_the_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("width = = 3")
    with pytest.raises(ConfigError, match=r"config\.toml"):
        load(path)


def test_show_age_must_be_bool():
    from sebside.config import ConfigError, parse

    assert parse({"show_age": False}).show_age is False
    assert parse({}).show_age is True
    with pytest.raises(ConfigError, match="show_age"):
        parse({"show_age": "no"})


def test_group_lines_default_file_env_and_minimum(tmp_path):
    old = tmp_path / "old.toml"
    old.write_text('width = 40\nsort = "name"\n')  # a config from before group_lines
    assert load(old).group_lines == 3 and load(old).width == 40
    assert parse({"group_lines": 1}).group_lines == 1
    assert load_from_env({"SEBSIDE_CONFIG": str(old), "SEBSIDE_GROUP_LINES": "5"}).group_lines == 5
    for bad in (0, "three", True):
        with pytest.raises(ConfigError, match="group_lines"):
            parse({"group_lines": bad})
