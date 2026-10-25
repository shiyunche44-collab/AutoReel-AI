"""素材覆盖的回归测试:每台车都得在片子里真的露过脸。

背景(实跑踩过):素材池采集与逐拍渲染是并行的,渲到第三、四台车时它们的
池子还没建好,那 8 拍就只剩一张卡片 —— 观众看到的是「这台车一个镜头都没有」。
门禁 A/B 都不查素材,于是这种片子一路走到发布,没人拦。
"""
from __future__ import annotations

import pytest

CARS = ["比亚迪秦L EV", "小鹏MONA M03", "银河A7 EM-i", "海豹06 DM-i"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    db.init_db()

    pid = "p_asset_cover_test"
    db.create_project({
        "id": pid, "title": "横评测试", "subject": "10-15万新能源轿车怎么选",
        "category": "auto_long", "theme": "priceband", "duration": 90, "shot_count": 4,
        "narration_mode": "tts", "state": "GATE_B", "state_reason": None,
        "quota_budget": 100.0, "spent_cost": 0.0, "est_cost": 1.0,
        "created_at": 1.0, "updated_at": 2.0,
        "subjects": [{"name": c, "group": "", "order": i} for i, c in enumerate(CARS)],
    })
    db.write_json(pid, "storyboard.json", [
        {"idx": i, "duration": 3.0, "section": "车型", "visual_zh": "x",
         "camera_zh": "Static Shot", "layout": "broll", "subject": car}
        for i, car in enumerate(CARS)
    ])
    return pid


def _specs():
    from app.models import ShotSpec

    return [ShotSpec(idx=i, duration=3.0, section="车型", visual_zh="x", narration_zh="旁白",
                     camera_zh="Static Shot", layout="broll", subject=car)
            for i, car in enumerate(CARS)]


def _pool(car):
    """给某台车建一个素材池目录(有没有池子决定这条检查管不管它)。"""
    from app.pipeline import library

    d = library.Library.dir_for(car)
    (d / "stills").mkdir(parents=True, exist_ok=True)
    return d


def _beat(project, idx, car, seconds=2.0):
    from app import config, db
    from app.pipeline import library

    path = library.Library.dir_for(car) / "stills" / f"img_{idx}.jpg"
    (config.project_dir(project) / "beats").mkdir(parents=True, exist_ok=True)
    db.write_json(project, f"beats/{idx:03d}.json", [
        {"item_id": f"img_{idx}", "kind": "still", "path": str(path),
         "start": 0.0, "length": seconds, "third_party": False}])


def test_coverage_flags_car_without_any_footage(project):
    """有素材池却一帧没用上 → 这台车在片子里是缺席的。"""
    from app.pipeline import explainer

    _pool("比亚迪秦L EV")
    _pool("银河A7 EM-i")
    _beat(project, 0, "比亚迪秦L EV")
    # 银河A7 EM-i 的拍全是卡片:beats 里没有它的素材

    cov = explainer.asset_coverage(project, _specs(), CARS, "10-15万新能源轿车怎么选")
    assert cov["missing"] == ["银河A7 EM-i"]
    assert "海豹06 DM-i" in cov["no_pool"], "没有池子的车不在这条检查里硬拦"
    row = next(r for r in cov["cars"] if r["name"] == "比亚迪秦L EV")
    assert row["beats"] == [0] and row["seconds"] == 2.0


def test_coverage_counts_footage_per_car(project):
    from app.pipeline import explainer

    for car in CARS:
        _pool(car)
    _beat(project, 0, "比亚迪秦L EV")
    _beat(project, 2, "银河A7 EM-i", 3.5)
    cov = explainer.asset_coverage(project, _specs(), CARS, "10-15万新能源轿车怎么选")
    assert cov["missing"] == ["小鹏MONA M03", "海豹06 DM-i"]
    row = next(r for r in cov["cars"] if r["name"] == "银河A7 EM-i")
    assert row["beats"] == [2] and row["seconds"] == 3.5


def test_gate_b_blocks_when_a_car_has_no_footage(project, monkeypatch):
    """门禁 B 必须拦住:某台车一个画面都没有。"""
    from app.pipeline import orchestrator

    _pool("银河A7 EM-i")
    monkeypatch.setattr(orchestrator.explainer, "promote_all", lambda *a, **k: 4)
    monkeypatch.setattr(orchestrator, "_specs", lambda pid: _specs())
    with pytest.raises(orchestrator.PipelineError) as err:
        orchestrator.advance(project, confirmed=True)
    assert "门禁 B 未放行" in str(err.value)
    assert "银河A7 EM-i" in str(err.value)


def test_gate_b_force_overrides_missing_footage(project, monkeypatch):
    from app.pipeline import orchestrator

    _pool("银河A7 EM-i")
    monkeypatch.setattr(orchestrator.explainer, "promote_all", lambda *a, **k: 4)
    monkeypatch.setattr(orchestrator, "_specs", lambda pid: _specs())
    out = orchestrator.advance(project, confirmed=True, force=True)
    assert out["state"] == "FINAL_RENDERING"


def test_pool_status_tells_building_from_empty(tmp_path, monkeypatch):
    """池子还在建 → 先别渲这一拍;建好了 → ready。"""
    from app.pipeline import library

    monkeypatch.setattr(library.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(library, "is_building", lambda subject: False)
    assert library.pool_status("没有这台车") == "none"

    (library.Library.dir_for("测试车型") / "stills").mkdir(parents=True, exist_ok=True)
    assert library.pool_status("测试车型") == "empty"

    monkeypatch.setattr(library, "is_building", lambda subject: True)
    assert library.pool_status("测试车型") == "building"
