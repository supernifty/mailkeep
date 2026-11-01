from datetime import date, timedelta

import pytest

from mailkeep.processors import reports
from test_processors import enabled


def points(values, *, start=date(2026, 1, 4)):
    return [{"start": (start + timedelta(days=7 * i)).isoformat(),
             "end": (start + timedelta(days=7 * (i + 1))).isoformat(), "value": value * 3600}
            for i, value in enumerate(values)]


@pytest.mark.parametrize("values,expected", [([10, 20, 30, 40, 50], [25, 35]),
                                           ([0, 0, 0, 4], [1]), ([3, 6, 9], [])])
def test_trailing_average_alignment_and_complete_windows(config_factory, monkeypatch, values, expected):
    from matplotlib.figure import Figure
    captured = []
    def capture(figure, *args, **kwargs):
        captured.extend((line.get_label(), list(line.get_xdata()), list(line.get_ydata()))
                        for line in figure.axes[0].lines)
    monkeypatch.setattr(Figure, "savefig", capture)
    config = enabled(config_factory())
    data = {"accepted": points(values)}
    reports.plot_history(config, config.processors.instances[0], data,
                         config.paths.database.parent / "trend.png")
    assert captured[0][2] == values
    trends = [line for line in captured if line[0] == "4-week moving average"]
    assert len(trends) == bool(expected)
    if expected:
        assert trends[0][2] == expected
        assert trends[0][1] == [date.fromisoformat(p["start"]) for p in data["accepted"][3:]]


def test_gap_restarts_smoothing_without_partial_windows(config_factory, monkeypatch):
    from matplotlib.figure import Figure
    captured = []
    monkeypatch.setattr(Figure, "savefig", lambda figure, *args, **kwargs:
                        captured.extend(list(line.get_ydata()) for line in figure.axes[0].lines))
    config = enabled(config_factory())
    data = {"accepted": points([1, 2, 3, 4]) + points([10, 20, 30], start=date(2026, 2, 8))}
    reports.plot_history(config, config.processors.instances[0], data,
                         config.paths.database.parent / "gap.png")
    assert captured == [[1, 2, 3, 4], [2.5], [10, 20, 30]]
