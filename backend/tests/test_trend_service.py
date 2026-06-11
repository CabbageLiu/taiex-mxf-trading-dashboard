from app.services.trend import classify, label_for


def test_label_strong_up():
    assert label_for(0.85) == "強勢上升"
    assert label_for(0.70) == "強勢上升"


def test_label_gentle_up():
    assert label_for(0.50) == "溫和上升"
    assert label_for(0.30) == "溫和上升"


def test_label_neutral():
    assert label_for(0.00) == "盤整"
    assert label_for(0.29) == "盤整"
    assert label_for(-0.29) == "盤整"


def test_label_gentle_down():
    assert label_for(-0.50) == "溫和下降"
    assert label_for(-0.30) == "溫和下降"


def test_label_strong_down():
    assert label_for(-0.85) == "強勢下降"
    assert label_for(-0.70) == "強勢下降"


def test_classify_bullish_strong():
    d, s, lab = classify(ema20=100, ema50=95, plus_di=30, minus_di=15, adx=40)
    assert d == 1
    assert s == round(1 * min(40 / 50, 1.0), 4) == 0.8
    assert lab == "強勢上升"


def test_classify_bearish_strong():
    d, s, lab = classify(ema20=95, ema50=100, plus_di=15, minus_di=30, adx=45)
    assert d == -1
    assert lab == "強勢下降"


def test_classify_sideways_mixed_ema_di():
    d, s, lab = classify(ema20=100, ema50=95, plus_di=15, minus_di=30, adx=40)
    assert d == 0
    assert s == 0
    assert lab == "盤整"


def test_classify_low_adx_caps_score():
    d, s, lab = classify(ema20=100, ema50=95, plus_di=30, minus_di=15, adx=10)
    assert d == 1
    assert s == 0.2
    assert lab == "盤整"  # score below 0.30 threshold


def test_score_at_boundary_0_70_strong_up():
    # 0.70 should be 強勢上升 (lower-bound inclusive)
    assert label_for(0.70) == "強勢上升"
    assert label_for(0.6999) == "溫和上升"
