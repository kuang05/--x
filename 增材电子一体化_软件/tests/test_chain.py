"""端到端链路测试：python3 -m unittest discover -s tests -v

覆盖 7.2 的关键口径：数量一致、连通性、初始分、切片层道次、
双哈希、Agent 提分与重放一致、缺陷三元绑定、版本不可篡改。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aepcb import agent, autoroute, board, gcode, kicad, mrc, printer, routing, slicing, step  # noqa: E402
from aepcb import slicecheck  # noqa: E402
from aepcb.driver import FakeMarlin, LoopbackTransport, MarlinDriver  # noqa: E402
from aepcb.store import DB, now  # noqa: E402
from fixtures import t19a  # noqa: E402


def _new_project(db: DB, name: str) -> tuple[str, str]:
    pid = db.ins("project", name=name, unit="mm", sheet_w_mm=60, sheet_h_mm=40,
                 origin_kind="board_bottom_left", data_schema_version=1,
                 dir_rel_path=f"p/{name}", created_at=now(), updated_at=now())
    v0 = db.ins("version", project_id=pid, version_no=0, kind="initial", author_kind="human",
                author_name="local", reason="init", created_at=now())
    db.c.execute("UPDATE project SET current_version_id=? WHERE id=?", (v0, pid))
    db.commit()
    return pid, v0


class ChainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = DB(Path(cls.tmp.name) / "app.db")
        db = cls.db
        cls.pid = db.ins("project", name="T19-A", unit="mm", sheet_w_mm=60, sheet_h_mm=40,
                         origin_kind="board_bottom_left", data_schema_version=1,
                         dir_rel_path="p", created_at=now(), updated_at=now())
        cls.v0 = db.ins("version", project_id=cls.pid, version_no=0, kind="initial",
                        author_kind="human", author_name="local", reason="init", created_at=now())
        db.c.execute("UPDATE project SET current_version_id=? WHERE id=?", (cls.v0, cls.pid))
        cls.sid, cls.model = board.import_step(db, cls.pid, step.make_fixture_step(60, 40, 1.2), step)
        cls.imp = board.import_board(db, cls.pid, cls.sid, t19a.t19a())
        cls.base = mrc.ensure_baseline(db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.tmp.cleanup()

    def test_01_step_bbox(self):
        x0, y0, z0, x1, y1, z1 = self.model.bbox
        self.assertAlmostEqual(x1 - x0, 60.0, places=3)
        self.assertAlmostEqual(y1 - y0, 40.0, places=3)
        self.assertEqual(self.model.units, "mm")

    def test_02_counts(self):
        self.assertEqual(self.imp.counts, {"components": 8, "pads": 24, "nets": 12, "routes": 12})

    def test_03_connectivity_pass(self):
        c = routing.connectivity(self.db, self.pid, self.v0, self.imp.circuit_id)
        self.assertEqual(c["result"], "pass")
        self.assertEqual((c["opens"], c["shorts"]), (0, 0))

    def test_04_initial_score_is_62(self):
        run = mrc.run(self.db, self.pid, self.v0)
        self.assertAlmostEqual(run["score"], 62.0, places=2)
        self.assertTrue({"R1", "R2", "R7", "R6"} <= {i["rule_code"] for i in run["issues"]})

    def test_05_mrc_idempotent(self):
        a = mrc.run(self.db, self.pid, self.v0)
        b = mrc.run(self.db, self.pid, self.v0)
        self.assertEqual(a["run_id"], b["run_id"])
        self.assertEqual(a["score"], b["score"])

    def test_06_slice_3_layers_7_passes(self):
        sl = slicing.run(self.db, self.pid, self.v0, self.base["process_profile_id"],
                         self.base["material_batch_id"])
        self.__class__.slice_id = sl["slice_id"]
        layers = self.db.scalar("SELECT COUNT(*) FROM physical_layer WHERE slice_set_id=?", (sl["slice_id"],), 0)
        passes = self.db.scalar("SELECT COUNT(*) FROM deposit_pass WHERE slice_set_id=?", (sl["slice_id"],), 0)
        self.assertEqual((layers, passes), (3, 7))
        self.assertAlmostEqual(sl["thickness_mm"], 0.44, places=3)
        stages = [r["stage"] for r in self.db.q(
            "SELECT stage FROM thermal_step WHERE slice_set_id=? ORDER BY step_index", (sl["slice_id"],))]
        self.assertEqual(stages, ["pre_gel", "silver_set", "cure", "final_cure", "cool"])

    def test_07_gcode_dual_hash(self):
        g1 = gcode.build(self.db, self.slice_id)
        g2 = gcode.build(self.db, self.slice_id)
        self.assertEqual(g1["content_hash"], g2["content_hash"])
        self.assertNotEqual(g1["file_hash"], g2["file_hash"])
        self.__class__.export_id = g1["export_id"]
        self.__class__.content_hash = g1["content_hash"]

    def test_08_print_and_defect(self):
        job = printer.start(self.db, self.pid, self.slice_id, self.export_id)
        out = printer.replay(self.db, job, inject_at={"layer": 2, "kind": "broken_line"})
        self.assertEqual(out["result"], "failed")
        d = printer.add_defect(self.db, job, "open", 2, 30.0, 20.0)
        a = printer.attribute(self.db, d)
        self.assertTrue(d and a)
        # 三元绑定：设计版本与切片不一致必须被拒
        sl = self.db.one("SELECT * FROM slice_set WHERE id=?", (self.slice_id,))
        with self.assertRaises(sqlite3.Error):
            self.db.ins("defect", print_job_id=job, slice_set_id=self.slice_id,
                        design_version_id="00000000-0000-4000-8000-000000000000",
                        defect_kind="short", severity="major", status="pending",
                        detected_at=now())

    def test_09_agent_improves_and_replays(self):
        before = mrc.run(self.db, self.pid, self.v0)["score"]
        out = agent.iterate(self.db, self.pid, self.v0)
        self.assertGreaterEqual(out["score_after"] - before, 15)
        self.assertLessEqual(out["rounds"], 8)
        rep = agent.replay(self.db, out["plan_id"], self.v0)
        self.assertTrue(rep["matched"])

    def test_10_version_immutable(self):
        with self.assertRaises(sqlite3.Error):
            self.db.c.execute("UPDATE version SET reason='tamper' WHERE id=?", (self.v0,))
        self.db.c.rollback()

    def test_11_autoroute_fills_gap(self):
        pid, v0 = _new_project(self.db, "autoroute")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        self.db.c.execute("UPDATE route SET deleted_at=? WHERE circuit_id=?", (now(), imp.circuit_id))
        self.db.commit()
        out = autoroute.route_all(self.db, imp.circuit_id, limit=12)
        self.assertGreaterEqual(out["rate"], 0.8, f"布通率过低：{out}")
        self.assertEqual(routing.connectivity(self.db, pid, v0, imp.circuit_id)["result"], "pass")

    def test_12_jumper_repairs_open(self):
        pid, v0 = _new_project(self.db, "jumper")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        net = self.db.one("SELECT * FROM net WHERE circuit_id=? ORDER BY name LIMIT 1", (imp.circuit_id,))
        r = self.db.one("SELECT * FROM route WHERE net_id=?", (net["id"],))
        pts = board.load_route_points(self.db, r)
        routing.set_points(self.db, r["id"], [(pts[0][0], pts[0][1]), (pts[0][0] + 0.3, pts[0][1])])
        self.db.commit()
        self.assertGreaterEqual(routing.connectivity(self.db, pid, v0, imp.circuit_id)["opens"], 1)
        jid = routing.suggest_jumper(self.db, imp.circuit_id, net["id"])
        routing.apply_jumper(self.db, jid)
        self.db.commit()
        self.assertEqual(routing.connectivity(self.db, pid, v0, imp.circuit_id)["result"], "pass")

    def test_13_kicad_roundtrip(self):
        path = Path(self.tmp.name) / "board.kicad_pcb"
        kicad.write_fixture_kicad(path, t19a.t19a())
        spec = kicad.parse_file(path)
        self.assertEqual(len(spec["components"]), 8)
        self.assertEqual(len(spec["pads"]), 24)
        self.assertGreaterEqual(len(spec["routes"]), 12)
        pid, v0 = _new_project(self.db, "kicad")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, spec)
        self.assertEqual(imp.counts["pads"], 24)
        self.assertEqual(routing.connectivity(self.db, pid, v0, imp.circuit_id)["result"], "pass")

    def test_14_web_state(self):
        from aepcb import web
        st = web.state(self.db)
        self.assertIn("board_svg", st)
        self.assertIn("iso_svg", st)
        self.assertTrue(st["project"])
        self.assertIn("分数", st["kv"])

    def test_15_slice_connectivity_blocks_export(self):
        pid, v0 = _new_project(self.db, "slicecheck")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        sl = slicing.run(self.db, pid, v0, self.base["process_profile_id"],
                         self.base["material_batch_id"])
        self.assertEqual(sl["connectivity"], "pass")
        # 人为删掉阻碍层路径：“切片后重算连通性”必须失败，并阻断导出
        pas = self.db.one("SELECT id FROM deposit_pass WHERE slice_set_id=? AND pattern='trench_fill'",
                          (sl["slice_id"],))
        self.db.c.execute("DELETE FROM slice_path WHERE deposit_pass_id=?", (pas["id"],))
        self.db.commit()
        conn = slicecheck.check(self.db, pid, v0, sl["slice_id"])
        self.assertEqual(conn["result"], "fail")
        with self.assertRaises(ValueError):
            gcode.build(self.db, sl["slice_id"])

    def test_16_marlin_driver_protocol(self):
        t = LoopbackTransport()
        fw = FakeMarlin(t)
        fw.start()
        d = MarlinDriver(t)
        self.assertIn("FakeMarlin", d.hello())
        self.assertEqual(d.query_temp()["hotend"], 25.0)
        d.home()
        d.preheat(80, 170)
        d.move(x=12.5, y=8.0, f=1200)
        d.stop()
        fw.join(2)
        self.assertGreaterEqual(d.stats.ok, 5)
        self.assertEqual(d.stats.errors, 0)
        self.assertTrue(any("G28" in c for c in fw.received))

    def test_18_undo_redo_keeps_version(self):
        from aepcb import history as H
        pid, v0 = _new_project(self.db, "history")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        route = self.db.one("SELECT * FROM route WHERE circuit_id=? ORDER BY id LIMIT 1",
                            (imp.circuit_id,))
        w0 = route["width_mm"]
        before_versions = self.db.scalar("SELECT count(*) FROM version WHERE project_id=?", (pid,), 0)
        H.change_width(self.db, pid, imp.circuit_id, route["id"], 0.9)
        self.assertAlmostEqual(self.db.one("SELECT width_mm FROM route WHERE id=?",
                                          (route["id"],))["width_mm"], 0.9, places=3)
        sess = H.ensure_session(self.db, pid)
        self.assertTrue(H.undo(self.db, sess))
        self.assertAlmostEqual(self.db.one("SELECT width_mm FROM route WHERE id=?",
                                          (route["id"],))["width_mm"], w0, places=3)
        self.assertEqual(self.db.scalar("SELECT count(*) FROM version WHERE project_id=?", (pid,), 0),
                         before_versions)   # PRJ-005：撤销不产生版本
        self.assertTrue(H.redo(self.db, sess))
        self.assertAlmostEqual(self.db.one("SELECT width_mm FROM route WHERE id=?",
                                          (route["id"],))["width_mm"], 0.9, places=3)

    def test_19_package_roundtrip(self):
        from aepcb import package
        pid, v0 = _new_project(self.db, "pkg")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        score = mrc.run(self.db, pid, v0)["score"]
        out = package.export(self.db, Path(self.tmp.name) / "pkg.zip")
        self.assertGreater(out["bytes"], 1000)
        self.assertGreater(out["blobs"], 0)
        restored = DB(Path(self.tmp.name) / "restored.db")
        try:
            manifest = package.import_package(restored, out["path"])
            self.assertEqual(manifest["counts"]["route"], out["counts"]["route"])
            self.assertEqual(manifest["counts"]["version"], out["counts"]["version"])
            self.assertEqual(mrc.run(restored, pid, v0)["score"], score)
            self.assertEqual(restored.scalar("SELECT count(*) FROM route"),
                             self.db.scalar("SELECT count(*) FROM route"))
        finally:
            restored.close()

    def test_20_doctor(self):
        from aepcb import doctor
        checks = doctor.run(Path(self.tmp.name) / "ws")
        ok, total, bad = doctor.summary(checks)
        self.assertEqual(bad, 0, f"必需项失败：{[c for c in checks if not c['ok']]}")
        names = {c["name"] for c in checks}
        self.assertTrue({"python", "numpy", "scipy", "sqlite", "schema", "workspace"} <= names)

    def test_21_web_history_stack(self):
        from aepcb import history as H
        from aepcb import web
        pid, v0 = _new_project(self.db, "hist")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        route = self.db.one("SELECT * FROM route WHERE circuit_id=? ORDER BY id LIMIT 1",
                            (imp.circuit_id,))
        H.change_width(self.db, pid, imp.circuit_id, route["id"], 0.9)
        st = web.state(self.db)
        self.assertTrue(any(not h["undone"] for h in st["history"]))
        self.assertTrue(all({"seq", "kind", "target", "undone"} <= set(h) for h in st["history"]))

    def test_22_via_connects_layers(self):
        spec = {
            "board": {"name": "via", "w": 60, "h": 40, "min_width": 0.5, "min_spacing": 0.5},
            "components": [{"ref": "U1", "x": 10, "y": 10, "rot": 0, "side": "top", "h": 1.2},
                           {"ref": "U2", "x": 50, "y": 30, "rot": 0, "side": "top", "h": 1.2}],
            "pads": [{"comp": "U1", "no": "1", "x": 10, "y": 10, "net": "N01"},
                     {"comp": "U2", "no": "1", "x": 50, "y": 30, "net": "N01"}],
            "nets": [{"name": "N01", "class": "default"}],
            "routes": [{"net": "N01", "layer": 0, "width": 0.6, "points": [[10, 10], [30, 20]]},
                       {"net": "N01", "layer": 1, "width": 0.6, "points": [[30, 20], [50, 30]]}],
            "vias": [], "keepouts": [],
        }
        pid, v0 = _new_project(self.db, "via")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, spec)
        before = routing.connectivity(self.db, pid, v0, imp.circuit_id)
        self.assertEqual(before["result"], "fail")      # 跨层无过孔 = 断路
        self.db.ins("via", circuit_id=imp.circuit_id, net_id=imp.net_ids["N01"],
                    x_mm=30, y_mm=20, diameter_mm=0.6, drill_mm=0.3,
                    from_layer=0, to_layer=1, created_at=now())
        self.db.commit()
        after = routing.connectivity(self.db, pid, v0, imp.circuit_id)
        self.assertEqual(after["result"], "pass")
        self.assertEqual(after["opens"], 0)

    def test_23_kicad_via_roundtrip(self):
        spec = t19a.t19a()
        spec["vias"] = [{"net": spec["routes"][0]["net"], "x": 30, "y": 20,
                         "diameter": 0.6, "drill": 0.3, "from_layer": 0, "to_layer": 1}]
        path = Path(self.tmp.name) / "via.kicad_pcb"
        kicad.write_fixture_kicad(path, spec)
        parsed = kicad.parse_file(path)
        self.assertEqual(len(parsed["vias"]), 1)
        self.assertAlmostEqual(parsed["vias"][0]["x"], 30, places=2)

    def test_24_undo_to_step(self):
        from aepcb import history as H
        pid, v0 = _new_project(self.db, "undo_to")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        route = self.db.one("SELECT * FROM route WHERE circuit_id=? ORDER BY id LIMIT 1",
                            (imp.circuit_id,))
        w0 = route["width_mm"]
        H.change_width(self.db, pid, imp.circuit_id, route["id"], 0.8)
        H.change_width(self.db, pid, imp.circuit_id, route["id"], 0.9)
        sess = H.ensure_session(self.db, pid)
        self.assertEqual(len(H.current_stack(self.db, pid)), 2)
        out = H.undo_to(self.db, sess, 0)
        self.assertEqual(out["undone"], 2)
        self.assertAlmostEqual(self.db.one("SELECT width_mm FROM route WHERE id=?",
                                          (route["id"],))["width_mm"], w0, places=3)
        self.assertTrue(all(h["undone"] for h in H.current_stack(self.db, pid)))

    def test_25_project_package_only_one_project(self):
        from aepcb import package
        pid_a, v_a = _new_project(self.db, "pkgA")
        sid_a, _ = board.import_step(self.db, pid_a, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid_a, sid_a, t19a.t19a())
        score = mrc.run(self.db, pid_a, v_a)["score"]
        pid_b, v_b = _new_project(self.db, "pkgB")
        sid_b, _ = board.import_step(self.db, pid_b, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid_b, sid_b, t19a.t19a())
        out = package.export_project(self.db, pid_a, Path(self.tmp.name) / "a.zip")
        self.assertEqual(out["scope"], "project")
        restored = DB(Path(self.tmp.name) / "restored_a.db")
        try:
            manifest = package.import_package(restored, out["path"])
            self.assertEqual(restored.scalar("SELECT count(*) FROM project"), 1)
            self.assertEqual(manifest["counts"]["route"], out["counts"]["route"])
            self.assertEqual(mrc.run(restored, pid_a, v_a)["score"], score)
        finally:
            restored.close()

    def test_26_net_batch_width(self):
        from aepcb import history as H
        from aepcb import routing as rt
        pid, v0 = _new_project(self.db, "netwidth")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        net = self.db.one("SELECT * FROM net WHERE circuit_id=? ORDER BY name LIMIT 1", (imp.circuit_id,))
        rt.add_route(self.db, imp.circuit_id, net["id"], [(28, 6), (32, 6)], 0.6)
        self.db.commit()
        out = H.change_net_width(self.db, pid, imp.circuit_id, net["id"], 0.9)
        self.assertGreaterEqual(out["routes"], 2)
        widths = [r["width_mm"] for r in self.db.q("SELECT width_mm FROM route WHERE net_id=?", (net["id"],))]
        self.assertTrue(all(abs(w - 0.9) < 1e-6 for w in widths))
        sess = H.ensure_session(self.db, pid)
        H.undo_to(self.db, sess, 0)
        widths = [r["width_mm"] for r in self.db.q("SELECT width_mm FROM route WHERE net_id=?", (net["id"],))]
        self.assertIn(0.6, [round(w, 3) for w in widths])

    def test_27_autoroute_creates_vias_on_layer_change(self):
        pid, v0 = _new_project(self.db, "autoroute_via")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        self.db.c.execute("UPDATE route SET deleted_at=? WHERE circuit_id=?", (now(), imp.circuit_id))
        self.db.commit()
        out = autoroute.route_all(self.db, imp.circuit_id, limit=12)
        self.assertGreaterEqual(out["rate"], 0.8, out)
        layer1 = [r for r in out["routed"] if r.get("layer") == 1]
        self.assertTrue(layer1, "本次没有换层布线，用例失去意义")
        vias = self.db.q("SELECT * FROM via WHERE circuit_id=?", (imp.circuit_id,))
        self.assertGreaterEqual(len(vias), 2 * len(layer1))
        self.assertEqual(routing.connectivity(self.db, pid, v0, imp.circuit_id)["result"], "pass")

    def test_28_slice_has_perimeters_before_infill(self):
        pid, v0 = _new_project(self.db, "perimeter")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        sl = slicing.run(self.db, pid, v0, self.base["process_profile_id"],
                         self.base["material_batch_id"])
        pas = self.db.one("SELECT id FROM deposit_pass WHERE slice_set_id=? AND pass_index=0",
                          (sl["slice_id"],))
        rows = self.db.q("SELECT * FROM slice_path WHERE deposit_pass_id=? AND path_kind='extrude' "
                         "ORDER BY seq", (pas["id"],))
        self.assertGreater(len(rows), 1)
        first = json.loads(self.db.get_blob(rows[0]["geom_blob_hash"]))
        last = json.loads(self.db.get_blob(rows[-1]["geom_blob_hash"]))
        # 首条是闭合轮廓（首尾点重合），末条是内填直线
        self.assertEqual((round(first[0][0], 3), round(first[0][1], 3)),
                         (round(first[-1][0], 3), round(first[-1][1], 3)))
        self.assertNotEqual((round(last[0][0], 3), round(last[0][1], 3)),
                            (round(last[-1][0], 3), round(last[-1][1], 3)))

    def test_29_travel_optimization_reduces_air_moves(self):
        pid, v0 = _new_project(self.db, "travel")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        base = self.base
        naive = slicing.run(self.db, pid, v0, base["process_profile_id"],
                            base["material_batch_id"], optimize_travel=False)
        best = slicing.run(self.db, pid, v0, base["process_profile_id"],
                           base["material_batch_id"], optimize_travel=True)
        def travel(slice_id):
            return self.db.scalar("SELECT COALESCE(SUM(sp.length_mm),0) FROM slice_path sp "
                                  "JOIN deposit_pass dp ON dp.id=sp.deposit_pass_id "
                                  "WHERE dp.slice_set_id=? AND sp.path_kind='travel'", (slice_id,), 0)
        self.assertLess(travel(best["slice_id"]), travel(naive["slice_id"]))
        self.assertEqual(best["connectivity"], "pass")

    def test_30_kicad_layer_mapping_and_blind_via(self):
        spec = t19a.t19a()
        spec["routes"] = [dict(r) for r in spec["routes"]]
        spec["routes"][0]["layer"] = 1
        spec["routes"][1]["layer"] = 15
        spec["vias"] = [{"net": spec["routes"][0]["net"], "x": 30, "y": 20,
                         "diameter": 0.6, "drill": 0.3, "from_layer": 0, "to_layer": 1}]
        path = Path(self.tmp.name) / "layers.kicad_pcb"
        kicad.write_fixture_kicad(path, spec)
        parsed = kicad.parse_file(path)
        layers = sorted({r["layer"] for r in parsed["routes"]})
        self.assertIn(1, layers)
        self.assertIn(15, layers)
        self.assertEqual(parsed["vias"][0]["from_layer"], 0)
        self.assertEqual(parsed["vias"][0]["to_layer"], 1)
        text = path.read_text(encoding="utf-8")
        self.assertIn('"In1.Cu"', text)
        self.assertIn('"B.Cu"', text)

    def test_31_infill_alternates_direction(self):
        pid, v0 = _new_project(self.db, "alt_infill")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        sl = slicing.run(self.db, pid, v0, self.base["process_profile_id"],
                         self.base["material_batch_id"])

        def infill_orientations(pass_index):
            pas = self.db.one("SELECT id FROM deposit_pass WHERE slice_set_id=? AND pass_index=?",
                              (sl["slice_id"], pass_index))
            rows = self.db.q("SELECT * FROM slice_path WHERE deposit_pass_id=? AND "
                             "path_kind='extrude' ORDER BY seq", (pas["id"],))
            horiz = vert = 0
            for r in rows:
                geom = json.loads(self.db.get_blob(r["geom_blob_hash"]))
                if len(geom) == 2:
                    if abs(geom[0][1] - geom[1][1]) < 1e-6:
                        horiz += 1
                    elif abs(geom[0][0] - geom[1][0]) < 1e-6:
                        vert += 1
            return horiz, vert

        h0, v0c = infill_orientations(0)
        h1, v1c = infill_orientations(1)
        self.assertGreater(h0, v0c)      # 道次 0 横向为主
        self.assertGreater(v1c, h1)      # 道次 1 纵向为主

    def test_32_gcode_feed_from_process_params(self):
        pid, v0 = _new_project(self.db, "feed")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        prof = self.db.one("SELECT * FROM process_profile WHERE id=?",
                           (self.base["process_profile_id"],))
        params = json.loads(prof["params_json"])
        params["speed_mm_s"] = 10.0
        new_id = self.db.ins("process_profile", project_id=pid, name="speed10",
                             params_json=json.dumps(params), params_hash="b" * 64,
                             is_valid=1, created_at=now())
        self.db.commit()
        sl = slicing.run(self.db, pid, v0, new_id, self.base["material_batch_id"])
        exp = gcode.build(self.db, sl["slice_id"])
        text = open(exp["path"], encoding="utf-8").read()
        self.assertIn("F600", text)      # 10 mm/s * 60
        self.assertNotIn("F1200", text)

    def test_33_delete_route_is_undoable(self):
        from aepcb import history as H
        from aepcb import web
        pid, v0 = _new_project(self.db, "delete_undo")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        route = self.db.one("SELECT * FROM route WHERE circuit_id=? ORDER BY id LIMIT 1",
                            (imp.circuit_id,))
        H.delete_route(self.db, pid, route["id"])
        alive = self.db.one("SELECT deleted_at FROM route WHERE id=?", (route["id"],))
        self.assertIsNotNone(alive["deleted_at"])
        sess = H.ensure_session(self.db, pid)
        self.assertTrue(H.undo(self.db, sess))
        back = self.db.one("SELECT deleted_at FROM route WHERE id=?", (route["id"],))
        self.assertIsNone(back["deleted_at"])
        st = web.state(self.db)
        self.assertTrue(any(r["id"] == route["id"] for r in st["routes"]))

    def test_34_cylindrical_surface_triggers_r3(self):
        pid, v0 = _new_project(self.db, "cylinder")
        board.import_step(self.db, pid,
                          step.make_fixture_step(60, 40, 1.2, with_cylinder=True), step)
        sid, _ = board.import_step(self.db, pid,
                                   step.make_fixture_step(60, 40, 1.2, with_cylinder=True), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        run = mrc.run(self.db, pid, v0)
        self.assertTrue(any(i["rule_code"] == "R3" for i in run["issues"]),
                        "圆柱面未被 R3 捕捉")

    def test_35_resume_recomputes_hold(self):
        pid, v0 = _new_project(self.db, "resume")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        sl = slicing.run(self.db, pid, v0, self.base["process_profile_id"],
                         self.base["material_batch_id"])
        exp = gcode.build(self.db, sl["slice_id"])
        job = printer.start(self.db, pid, sl["slice_id"], exp["export_id"])
        printer.pause(self.db, job, kind="curing_pause", remaining_hold_s=180)
        out = printer.resume(self.db, job)
        self.assertTrue(out["resumed"])
        self.assertGreaterEqual(out["remaining_hold_s"], 180)
        st = self.db.one("SELECT status FROM print_job WHERE id=?", (job,))
        self.assertEqual(st["status"], "running")
        ev = self.db.one("SELECT * FROM print_event WHERE print_job_id=? AND code='resume'", (job,))
        self.assertIsNotNone(ev)

    def test_36_agent_fixes_r4_and_report_has_trend(self):
        from aepcb import agent as A
        from aepcb import report
        pid, v0 = _new_project(self.db, "r4")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        # 本体 16 mm：N05（y=28）刚好落在本体外缘，触发 R4 间隙不足（而非 R5 遮挡）
        self.db.c.execute("UPDATE component SET body_h_mm=16 WHERE circuit_id=? AND ref_des='U1'",
                          (imp.circuit_id,))
        self.db.commit()
        before = mrc.run(self.db, pid, v0)
        self.assertTrue(any(i["rule_code"] == "R4" for i in before["issues"]), before["issues"])
        out = A.iterate(self.db, pid, v0)
        self.assertGreater(out["score_after"], before["score"])
        html = report.build(self.db, pid, Path(self.tmp.name) / "r.html").read_text(encoding="utf-8")
        self.assertIn("分数趋势", html)

    def test_37_web_project_switch(self):
        from aepcb import web
        pid_a, _ = _new_project(self.db, "switchA")
        pid_b, _ = _new_project(self.db, "switchB")
        st = web.state(self.db)
        self.assertGreaterEqual(len(st["projects"]), 2)
        # 默认展示最近创建的 B
        self.assertEqual(st["project"], "switchB")
        self.db.set_setting("ui.current_project", pid_a)
        self.db.commit()
        st2 = web.state(self.db)
        self.assertEqual(st2["project"], "switchA")

    def test_38_kicad_rotated_footprint_pads(self):
        text = '''(kicad_pcb (version 20221018)
  (net 1 "N01")
  (footprint "QFN" (layer "F.Cu") (at 10 10 90) (property "Reference" "U1")
    (pad "1" smd rect (at 2 0) (size 1 1) (net 1 "N01"))
    (pad "2" smd rect (at 0 2) (size 1 1) (net 1 "N01")))
)'''
        spec = kicad.parse(text)
        pads = {p["no"]: p for p in spec["pads"]}
        # 90° 旋转：(2,0) -> (0,2)，(0,2) -> (-2,0)
        self.assertAlmostEqual(pads["1"]["x"], 10.0, places=2)
        self.assertAlmostEqual(pads["1"]["y"], 12.0, places=2)
        self.assertAlmostEqual(pads["2"]["x"], 8.0, places=2)
        self.assertAlmostEqual(pads["2"]["y"], 10.0, places=2)

    def test_39_kicad_non_90_rotation_warns(self):
        text = '''(kicad_pcb (version 20221018)
  (footprint "QFN" (layer "F.Cu") (at 5 5 45) (property "Reference" "U9"))
)'''
        spec = kicad.parse(text)
        self.assertEqual(spec["components"][0]["rot"], 0)   # 45° → 归一到 0
        self.assertTrue(spec["rotation_warnings"])
        self.assertAlmostEqual(spec["rotation_warnings"][0]["raw_rot"], 45.0, places=1)

    def test_40_cli_chain(self):
        import subprocess
        ws = Path(self.tmp.name) / "cliws"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "board.step").write_text(step.make_fixture_step(60, 40, 1.2), encoding="utf-8")
        (ws / "board.json").write_text(json.dumps(t19a.t19a(), ensure_ascii=False), encoding="utf-8")

        def cli(*args):
            out = subprocess.run([sys.executable, "-m", "aepcb.cli", "--workspace", str(ws), *args],
                                 capture_output=True, text=True, cwd=str(ROOT))
            self.assertEqual(out.returncode, 0, out.stderr[-400:])
            return out.stdout

        self.assertIn("工程已创建", cli("new", "--name", "cli测试"))
        self.assertIn("板文件", cli("import", "--step", str(ws / "board.step"),
                                  "--board", str(ws / "board.json")))
        self.assertIn("切片", cli("slice"))
        self.assertIn("content=", cli("export"))
        self.assertIn("工程包", cli("package", "--out", str(ws / "p.aepkg")))
        self.assertTrue((ws / "p.aepkg").exists())

    def test_41_rule_versioning(self):
        pid, v0 = _new_project(self.db, "rulever")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        base_run = mrc.run(self.db, pid, v0)
        base_score = base_run["score"]
        out = mrc.create_rule_version(self.db, "v2", {"R1.min_width_mm": 0.3})
        self.assertTrue(out["rule_version_id"])
        loose = mrc.run(self.db, pid, v0, rule_version_id=out["rule_version_id"])
        self.assertGreater(loose["score"], base_score)   # 阈值放宽 → R1 不再报 → 分数升
        # 旧运行仍在，分数不被覆盖（MRC-011）
        old = self.db.one("SELECT total_score FROM mrc_run WHERE id=?", (base_run["run_id"],))
        self.assertAlmostEqual(old["total_score"], base_score, places=2)
        versions = mrc.list_rule_versions(self.db)
        self.assertGreaterEqual(len(versions), 2)
        self.assertTrue(any(v["is_active"] for v in versions))

    def test_42_draw_on_layer_creates_vias(self):
        from aepcb import routing as rt
        pid, v0 = _new_project(self.db, "draw_layer")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        net = self.db.one("SELECT * FROM net WHERE circuit_id=? ORDER BY name LIMIT 1", (imp.circuit_id,))
        before = self.db.scalar("SELECT count(*) FROM via WHERE circuit_id=?", (imp.circuit_id,), 0)
        rt.add_route(self.db, imp.circuit_id, net["id"], [(30, 6), (36, 6)], 0.6, layer=1)
        self.db.commit()
        after = self.db.scalar("SELECT count(*) FROM via WHERE circuit_id=?", (imp.circuit_id,), 0)
        self.assertEqual(after - before, 2)     # 两端各一个过孔
        conn = rt.connectivity(self.db, pid, v0, imp.circuit_id)
        self.assertEqual(conn["result"], "pass")

    def test_43_package_merge_id_remap(self):
        from aepcb import package
        pid, v0 = _new_project(self.db, "mergeB")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        score = mrc.run(self.db, pid, v0)["score"]
        routes_before = self.db.scalar("SELECT count(*) FROM route", (), 0)
        projects_before = self.db.scalar("SELECT count(*) FROM project", (), 0)
        out = package.export_project(self.db, pid, Path(self.tmp.name) / "merge.aepkg")
        res = package.import_package(self.db, out["path"], merge=True)
        self.assertTrue(res["merged"])
        self.assertEqual(self.db.scalar("SELECT count(*) FROM project", (), 0), projects_before + 1)
        self.assertEqual(self.db.scalar("SELECT count(*) FROM route", (), 0),
                         routes_before + out["counts"]["route"],
                         f"导出 {out['counts']['route']} 条，导入前 {routes_before} 条")
        new_pid = res["projects"][0]
        self.assertNotEqual(new_pid, pid)          # id 已重映射
        new_v = self.db.scalar("SELECT id FROM version WHERE project_id=? ORDER BY version_no LIMIT 1",
                               (new_pid,))
        self.assertAlmostEqual(mrc.run(self.db, new_pid, new_v)["score"], score, places=2)

    def test_44_agent_fixes_r5_occlusion(self):
        from aepcb import agent as A
        pid, v0 = _new_project(self.db, "r5")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        # 把 U1 本体撑到 24 mm，使 N05（y=28）穿过其本体 → R5 遮挡
        self.db.c.execute("UPDATE component SET body_h_mm=24 WHERE circuit_id=? AND ref_des='U1'",
                          (imp.circuit_id,))
        self.db.commit()
        before = mrc.run(self.db, pid, v0)
        self.assertTrue(any(i["rule_code"] == "R5" for i in before["issues"]), before["issues"])
        out = A.iterate(self.db, pid, v0)
        self.assertGreater(out["score_after"], before["score"])
        self.assertLessEqual(out["rounds"], 8)

    def test_45_evidence_bundle(self):
        from aepcb import agent as A
        from aepcb import evidence as E
        pid, v0 = _new_project(self.db, "evidence")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        mrc.run(self.db, pid, v0)
        sl = slicing.run(self.db, pid, v0, self.base["process_profile_id"],
                         self.base["material_batch_id"])
        gcode.build(self.db, sl["slice_id"])
        gcode.build(self.db, sl["slice_id"])
        A.iterate(self.db, pid, v0)
        out = E.write(self.db, pid, Path(self.tmp.name) / "ev.md")
        text = Path(out["markdown"]).read_text(encoding="utf-8")
        for section in ("验收证据包", "可打性", "切片与导出", "连通性检查", "Agent 计划", "验收结果"):
            self.assertIn(section, text)
        self.assertIn("导出记录按内容哈希去重", text)
        data = json.loads(Path(out["json"]).read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["name"], "evidence")
        self.assertTrue(data["runs"])
        self.assertTrue(data["exports"])

    def test_46_accept_writes_evidence(self):
        from aepcb import accept as AC
        from aepcb import evidence as E
        pid, v0 = _new_project(self.db, "auto_evidence")
        self.db.ins("accept_result", task_no="T19", project_id=pid,
                    input_blob_hash="c" * 64, passed=1, rounds=4,
                    score_initial=62, score_final=97, ran_at=now())
        self.db.commit()
        path = AC.write_evidence(self.db, pid)
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("T19", text)
        self.assertIn("验收结果", text)
        self.assertTrue(Path(path).with_suffix(".json").exists())

    def test_47_per_pass_speed(self):
        pid, v0 = _new_project(self.db, "pass_speed")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        prof = self.db.one("SELECT * FROM process_profile WHERE id=?",
                           (self.base["process_profile_id"],))
        params = json.loads(prof["params_json"])
        params["speed_mask_mm_s"] = 12.0
        params["speed_silver_mm_s"] = 6.0
        new_id = self.db.ins("process_profile", project_id=pid, name="two-speed",
                             params_json=json.dumps(params), params_hash="d" * 64,
                             is_valid=1, created_at=now())
        self.db.commit()
        sl = slicing.run(self.db, pid, v0, new_id, self.base["material_batch_id"])
        rows = self.db.q(
            "SELECT dp.pattern, sp.speed_mm_s FROM slice_path sp "
            "JOIN deposit_pass dp ON dp.id=sp.deposit_pass_id "
            "WHERE dp.slice_set_id=? AND sp.path_kind='extrude'", (sl["slice_id"],))
        by_pattern = {}
        for r in rows:
            by_pattern.setdefault(r["pattern"], r["speed_mm_s"])
        self.assertAlmostEqual(by_pattern["solid"], 12.0, places=2)      # 介电
        self.assertAlmostEqual(by_pattern["trench_fill"], 6.0, places=2)  # 银浆
        exp = gcode.build(self.db, sl["slice_id"])
        text = open(exp["path"], encoding="utf-8").read()
        self.assertIn("F360", text)   # 银浆 6 mm/s → 360
        self.assertIn("F720", text)   # 介电 12 mm/s → 720

    def test_17_web_route_api(self):
        from aepcb import routing as rt
        from aepcb import web
        pid, _ = _new_project(self.db, "webroute")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        net = self.db.one("SELECT * FROM net WHERE circuit_id=? ORDER BY name LIMIT 1", (imp.circuit_id,))
        rid = rt.add_route(self.db, imp.circuit_id, net["id"], [(30, 5), (35, 5)], 0.6)
        self.db.commit()
        st = web.state(self.db)
        self.assertTrue(any(r["id"] == rid for r in st["routes"]))
        with self.assertRaises(ValueError):
            rt.add_route(self.db, imp.circuit_id, net["id"], [(10, 10), (10, 10)], 0.3)

    def test_48_surrogate_ranking(self):
        """代理模型：只做候选粗排，指标必须当场可复现且无分组泄漏。"""
        import tempfile
        from pathlib import Path
        from aepcb import surrogate
        with tempfile.TemporaryDirectory() as tmp:
            rep = surrogate.run(Path(tmp), groups=24, per_group=5)
            strict = rep["group_split"]
            self.assertGreaterEqual(strict["spearman"], 0.7)      # 分组留出后的排序相关性
            # 口径包含**特征提取 + 预测**（v2 只计 predict，报出的 3000× 属错误口径）。
            # 这条断言只用于防止“代理比规则还慢”的退化；单次采样在共享 CPU 上
            # 实测 2.4–12× 抳动，精确数字看 surrogate_report.md 的中位数与端到端。
            self.assertGreaterEqual(strict["speedup"], 2.0)
            self.assertGreaterEqual(strict["speedup_e2e"], 1.5)   # 含应用改动的端到端
            self.assertLessEqual(strict["mae"], 15.0)             # 分数平均误差
            self.assertIsNotNone(strict["top1_pairwise"])        # 成对模型已训练
            self.assertTrue(Path(tmp, "surrogate_report.md").exists())
            self.assertTrue(Path(tmp, "surrogate.pkl").exists())

    def test_49_agent_candidate_ranking(self):
        """候选粗排：代理只定顺序，应用后仍由规则复检并收敛。"""
        import tempfile
        from pathlib import Path
        from aepcb import agent, surrogate
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            surrogate.run(ws, groups=10, per_group=4)
            bundle = surrogate.load(ws)
            self.assertIsNotNone(bundle, "应能从 workspace 读回模型")
            pid, v0 = _new_project(self.db, "cand")
            sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
            board.import_board(self.db, pid, sid, t19a.t19a())
            out = agent.iterate(self.db, pid, v0, candidates=4, surrogate_bundle=bundle)
            self.assertGreaterEqual(out["score_after"], 95.0)
            rows = self.db.q("SELECT params_json FROM plan_step WHERE plan_id=?",
                             (out["plan_id"],))
            self.assertTrue(any('"ranked_by": "surrogate"' in (r["params_json"] or "")
                                for r in rows), "步骤应记录粗排来源")

    def test_50_width_sweep(self):
        """宽度扫描：代理只排序，最终选择仍由规则给分。"""
        import tempfile
        from pathlib import Path
        from aepcb import sweep, surrogate
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            surrogate.run(ws, groups=10, per_group=5)
            bundle = surrogate.load(ws)
            self.assertIsNotNone(bundle)
            pid, v0 = _new_project(self.db, "sweep")
            sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
            imp = board.import_board(self.db, pid, sid, t19a.t19a())
            out = sweep.width_sweep(self.db, pid, v0, imp.circuit_id,
                                    widths=(0.5, 0.6, 0.7, 0.8), bundle=bundle)
            self.assertEqual(len(out["results"]), 4)
            self.assertIn("best_surrogate", out)
            self.assertIsInstance(out["agree"], bool)
            self.assertGreater(out["speedup"], 1.0, "代理粗排应比穷举快")
            # 与穷举口径对照：代理选中的宽度，其规则分数必须能被复算出来
            picked = out["best_surrogate"]["width_mm"]
            row = [r for r in out["results"] if r["width_mm"] == picked][0]
            self.assertEqual(row["score"], out["best_surrogate"]["score"])

    def test_51_width_suggest_keeps_geometry(self):
        """界面「建议宽度」：只给建议，不得改动几何。"""
        from aepcb import web
        pid, v0 = _new_project(self.db, "suggest")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        rows = self.db.q("SELECT id, width_mm FROM route WHERE circuit_id=? "
                         "AND deleted_at IS NULL LIMIT 3", (imp.circuit_id,))
        before = {r["id"]: float(r["width_mm"]) for r in rows}
        h = web.Handler.__new__(web.Handler)
        h.db = self.db
        self.db.set_setting("ui.current_project", pid)   # 不依赖其他用例留下的选中工程
        self.db.commit()
        out = h._width_suggest({"route_ids": list(before)})
        self.assertIn("width_mm", out)
        self.assertGreater(out["width_mm"], 0)
        self.assertIn(out["mode"], ("穷举", "代理粗排"))
        after = {r["id"]: float(r["width_mm"]) for r in self.db.q(
            "SELECT id, width_mm FROM route WHERE id IN (%s)" % ",".join("?" * len(before)),
            tuple(before))}
        self.assertEqual(before, after, "建议操作不得修改几何")

    def test_52_iso_geometry_for_viewer(self):
        """3D 视图：服务端只给 3D 点，投影与交互在前端。"""
        from aepcb import report, web
        pid, v0 = _new_project(self.db, "iso")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        g = report.iso_geometry(self.db, pid)
        self.assertEqual(g["board"], [60.0, 40.0])
        self.assertTrue(g["routes"], "应有走线几何")
        for r in g["routes"]:
            self.assertEqual(len(r["pts"][0]), 3, "每个点必须是 x,y,z")
            self.assertIn(r["color"], ("#F87171", "#34D399"))
        self.assertTrue(g["comps"] and g["pads"], "应有器件与焊盘")
        self.db.set_setting("ui.current_project", pid)
        self.db.commit()
        st = web.state(self.db)
        self.assertIn("iso", st, "state 必须带 iso，前端才能实时投影")
        self.assertEqual(st["iso"]["board"], [60.0, 40.0])

    def test_53_viewer_drag_targets(self):
        """2D 视图必须给可拖拽的命中线与拐点数据。"""
        from aepcb import report, web
        pid, v0 = _new_project(self.db, "drag")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        svg = report._svg_board(self.db, pid, width=760)
        self.assertIn('class="route-hit"', svg, "缺失命中线，鼠标点不中 0.6 mm 的走线")
        self.assertIn('class="route-line"', svg)
        self.assertIn('data-id=', svg)
        self.db.set_setting("ui.current_project", pid)
        self.db.commit()
        st = web.state(self.db)
        r0 = st["routes"][0]
        self.assertIn("pts", r0, "拖拽需要拐点坐标")
        self.assertGreaterEqual(len(r0["pts"]), 2)
        self.assertEqual(len(r0["pts"][0]), 2)

    def test_54_panels_have_data(self):
        """UI-004：计划/步骤/对比卡片/打印面板必须有数据源。"""
        from aepcb import agent, gcode, mrc, printer, slicing, web
        pid, v0 = _new_project(self.db, "panels")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        agent.iterate(self.db, pid, v0)
        base = mrc.ensure_baseline(self.db)
        sl = slicing.run(self.db, pid, v0, base["process_profile_id"], base["material_batch_id"])
        exp = gcode.build(self.db, sl["slice_id"])
        printer.run_demo(self.db, pid, sl["slice_id"], exp["export_id"], fault_layer=2)
        self.db.set_setting("ui.current_project", pid)
        self.db.commit()
        st = web.state(self.db)
        self.assertIsNotNone(st["plan"], "计划面板无数据源")
        self.assertGreaterEqual(len(st["steps"]), 1, "步骤表为空")
        self.assertGreaterEqual(len(st["diffs"]), 1, "对比卡片为空")
        self.assertIsNotNone(st["print"], "打印面板无数据源")
        self.assertIn("layer", st["print"])
        self.assertIn("status", st["print"])

    def test_55_five_zone_layout(self):
        """UI-001：五区齐全，层开关在底栏，左栏四域有锚点。"""
        from aepcb import web
        html = web.INDEX
        for token in ('id="navPanel"', 'id="canvasPanel"', 'id="asidePanel"',
                      '<footer>', 'id="dev"', 'id="timeline"', 'id="progressBar"'):
            self.assertIn(token, html, f"缺五区元素 {token}")
        # 层开关只能有一处（已从工具条移到 footer，重复会让显隐失控）
        self.assertEqual(html.count('id="layer0"'), 1)
        self.assertEqual(html.count('id="layer1"'), 1)
        for hid in ("hRoutes", "hIssues", "hPlan", "hPrint", "hLog"):
            self.assertIn(f'id="{hid}"', html, f"左栏四域缺锚点 {hid}")
        pid, v0 = _new_project(self.db, "layout")
        self.db.set_setting("ui.current_project", pid)
        self.db.commit()
        st = web.state(self.db)
        self.assertIn("device", st, "顶栏设备字段无数据源")

    def test_56_shortcuts(self):
        """UI-006：快捷键已绑定，且输入框聚焦时放行。"""
        from aepcb import web
        html = web.INDEX
        self.assertEqual(html.count("addEventListener('keydown'"), 1, "快捷键应只绑一次")
        for key in ("'Escape'", "'Delete'", "'Tab'", "ctrlKey", "metaKey"):
            self.assertIn(key, html, f"缺快捷键 {key}")
        self.assertIn("tagName==='INPUT'", html, "输入框聚焦时必须放行，否则打字会触发删除")

    def test_57_cut_and_measure(self):
        """STR-003：剝切与测量控件必须存在，且测距常数与文档一致。"""
        import math
        from aepcb import web
        html = web.INDEX
        for token in ('id="cutZ"', 'id="measureMode"', 'id="measureOut"', 'id="cutOut"'):
            self.assertIn(token, html, f"缺剝切/测量控件 {token}")
        self.assertIn("cutZ + 1e-9", html, "剝切必须真的过滤层，不能只改滑块")
        self.assertIn("Math.hypot", html, "测量必须用两点距离")
        # 文档里写的 72.11 mm 必须与真实欧氏距离一致（防手写错）
        self.assertAlmostEqual(math.hypot(60, 40), 72.11, places=2)

    def test_58_component_ops(self):
        """CIR-002：移动/旋转/翻面/锁定 + 撤销，焊盘必须同步。"""
        import math
        from aepcb import history, routing
        pid, v0 = _new_project(self.db, "comp")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        c = self.db.one("SELECT * FROM component WHERE circuit_id=? ORDER BY ref_des LIMIT 1",
                        (imp.circuit_id,))
        before = routing.component_snapshot(self.db, c["id"])
        history.component_op(self.db, pid, imp.circuit_id, c["id"], "move", dx=5, dy=3)
        after = routing.component_snapshot(self.db, c["id"])
        self.assertAlmostEqual(after["x_mm"], before["x_mm"] + 5, places=6)
        for p0, p1 in zip(before["pads"], after["pads"]):
            self.assertAlmostEqual(p1["x_mm"], p0["x_mm"] + 5, places=6)
            self.assertAlmostEqual(p1["y_mm"], p0["y_mm"] + 3, places=6)
        # 旋转 90°：刚体旋转，焊盘到中心的距离必须不变
        mid = routing.component_snapshot(self.db, c["id"])
        history.component_op(self.db, pid, imp.circuit_id, c["id"], "rotate", deg=90)
        rot = routing.component_snapshot(self.db, c["id"])
        self.assertEqual(rot["rot_deg"], (mid["rot_deg"] + 90) % 360)
        for p0, p1 in zip(mid["pads"], rot["pads"]):
            d0 = math.hypot(p0["x_mm"] - mid["x_mm"], p0["y_mm"] - mid["y_mm"])
            d1 = math.hypot(p1["x_mm"] - rot["x_mm"], p1["y_mm"] - rot["y_mm"])
            self.assertAlmostEqual(d0, d1, places=3)
        # 翻面：side 取反
        history.component_op(self.db, pid, imp.circuit_id, c["id"], "flip")
        flipped = routing.component_snapshot(self.db, c["id"])
        self.assertNotEqual(flipped["side"], rot["side"])
        # 锁定后移动必须拒绝
        history.component_op(self.db, pid, imp.circuit_id, c["id"], "lock", locked=True)
        with self.assertRaises(ValueError):
            history.component_op(self.db, pid, imp.circuit_id, c["id"], "move", dx=1, dy=0)
        # 撤销一步：锁定被回滚
        session = history.ensure_session(self.db, pid)
        history.undo(self.db, session)
        locked = int(self.db.one("SELECT locked FROM component WHERE id=?", (c["id"],))["locked"])
        self.assertEqual(locked, 0)

    def test_59_job_progress_and_cancel(self):
        """UI-005：进度单调、可取消；取消后回滚回调必须执行；切片真会报进度。"""
        import time
        from aepcb import jobs, mrc, slicing
        reg = jobs.Registry()
        seen = []

        def work(job):
            for i in range(4):
                job.progress, job.total = i, 4
                seen.append(i)
                time.sleep(0.005)
            return {"slice_id": "x", "paths": 1}

        jid = reg.submit("demo", work)
        job = None
        for _ in range(400):
            job = reg.get(jid)
            if job.status != "running":
                break
            time.sleep(0.005)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.snapshot()["progress"], 100)
        self.assertEqual(seen, [0, 1, 2, 3])

        rolled = {"n": 0}

        def slow(job):
            for _ in range(200):
                if job.cancelled():
                    raise RuntimeError("cancelled")
                time.sleep(0.005)
            return {}

        jid2 = reg.submit("slow", slow,
                          rollback=lambda: rolled.__setitem__("n", rolled["n"] + 1))
        self.assertTrue(reg.cancel(jid2))
        for _ in range(400):
            j2 = reg.get(jid2)
            if j2.status != "running":
                break
            time.sleep(0.005)
        self.assertEqual(j2.status, "cancelled")
        self.assertEqual(rolled["n"], 1, "取消后必须执行回滚")

        # 真实切片：进度回调确实被调用（7 个道次）
        pid, v0 = _new_project(self.db, "prog")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        base = mrc.ensure_baseline(self.db)
        calls = []
        slicing.run(self.db, pid, v0, base["process_profile_id"], base["material_batch_id"],
                    progress=lambda i, t: calls.append((i, t)))
        self.assertGreaterEqual(len(calls), 7)
        self.assertEqual(calls[0], (0, 7))

    def test_60_component_snap(self):
        """CIR-002 栅格吸附：位移按 snap 取整（服务端做，界面与 API 一致）。"""
        from aepcb import routing
        pid, v0 = _new_project(self.db, "snap")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, t19a.t19a())
        c = self.db.one("SELECT * FROM component WHERE circuit_id=? ORDER BY ref_des LIMIT 1",
                        (imp.circuit_id,))
        x0, y0 = float(c["x_mm"]), float(c["y_mm"])
        routing.move_component(self.db, imp.circuit_id, c["id"], 1.7, 0.3, snap_mm=0.5)
        row = self.db.one("SELECT x_mm, y_mm FROM component WHERE id=?", (c["id"],))
        self.assertAlmostEqual(float(row["x_mm"]) - x0, 1.5, places=6)
        self.assertAlmostEqual(float(row["y_mm"]) - y0, 0.5, places=6)
        # 不传 snap 时保留原位移
        routing.move_component(self.db, imp.circuit_id, c["id"], 0.3, 0.0)
        row2 = self.db.one("SELECT x_mm FROM component WHERE id=?", (c["id"],))
        self.assertAlmostEqual(float(row2["x_mm"]) - float(row["x_mm"]), 0.3, places=6)

    def test_61_chat_panel(self):
        """UI-001 右栏对话：由计划/步骤/问题合成，不依赖外部服务。"""
        from aepcb import agent, web
        pid, v0 = _new_project(self.db, "chat")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        agent.iterate(self.db, pid, v0)
        self.db.set_setting("ui.current_project", pid)
        self.db.commit()
        st = web.state(self.db)
        self.assertIn("chat", st)
        self.assertTrue(st["chat"], "对话面板不应为空")
        roles = {m["role"] for m in st["chat"]}
        self.assertTrue(roles & {"user", "assistant", "system"})
        self.assertTrue(all(m.get("text") for m in st["chat"]), "每条消息必须有文本")

    def test_62_evidence_sections(self):
        """证据包必须有 8 节与运行环境字段（缺节等于答辩时讲不全）。"""
        from aepcb import evidence
        pid, v0 = _new_project(self.db, "ev")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        board.import_board(self.db, pid, sid, t19a.t19a())
        out = evidence.write(self.db, pid, self.db.root / "evidence_test.md")
        with open(out["markdown"], encoding="utf-8") as f:
            md = f.read()
        for sec in ("## 0. 运行环境", "## 1. 可打性", "## 2. 切片与导出",
                    "## 3. 连通性检查", "## 4. Agent 计划", "## 5. 验收结果",
                    "## 6. UI 性能", "## 7. 代理模型"):
            self.assertIn(sec, md, f"证据包缺 {sec}")
        self.assertIn("CPU：", md)
        self.assertIn("Python：", md)
        self.assertIn("SQLite：", md)

    def test_63_copper_zone_short(self):
        """KiCad zone：铺铜入库，异网重叠必须报 R2 短路。"""
        from aepcb import kicad, mrc
        text = '''(kicad_pcb (version 20240108)
          (net 0 "") (net 1 "GND") (net 2 "SIG")
          (footprint "R" (layer "F.Cu") (at 10 10)
            (fp_text reference "R1" (at 0 0))
            (pad "1" smd rect (at -0.8 0) (size 0.8 0.9) (layers "F.Cu") (net 2 "SIG"))
            (pad "2" smd rect (at 0.8 0) (size 0.8 0.9) (layers "F.Cu") (net 1 "GND")))
          (segment (start 10 10) (end 20 10) (width 0.25) (layer "F.Cu") (net 2))
          (zone (net 1) (layer "F.Cu") (priority 1)
            (polygon (pts (xy 15 8) (xy 30 8) (xy 30 20) (xy 15 20)))))
        '''
        spec = kicad.parse(text)
        self.assertEqual(len(spec["zones"]), 1, "zone 应被解析")
        self.assertEqual(len(spec["zones"][0]["points"]), 4)
        pid, v0 = _new_project(self.db, "zone")
        sid, _ = board.import_step(self.db, pid, step.make_fixture_step(60, 40, 1.2), step)
        imp = board.import_board(self.db, pid, sid, spec)
        n = self.db.scalar("SELECT COUNT(*) FROM copper_zone WHERE circuit_id=?",
                           (imp.circuit_id,), 0)
        self.assertEqual(n, 1, "铺铜应落库")
        run = mrc.run(self.db, pid, v0)
        hits = [i for i in run["issues"] if "铺铜" in i["message"]]
        self.assertTrue(hits, "异网走线穿过铺铜必须报短路")
        self.assertIn("重叠", hits[0]["message"])

    def test_64_arc_route_discretized(self):
        """弧形走线：三点弧必须离散成折线，否则长度与间距都算错。"""
        from aepcb import kicad
        text = '''(kicad_pcb (version 20240108)
          (net 0 "") (net 2 "SIG")
          (arc (start 20 10) (mid 25 15) (end 20 20) (width 0.25) (layer "F.Cu") (net 2)))
        '''
        spec = kicad.parse(text)
        arcs = [r for r in spec["routes"] if r.get("arc")]
        self.assertEqual(len(arcs), 1)
        pts = arcs[0]["points"]
        self.assertGreaterEqual(len(pts), 8, "弧线应被离散成多点")
        self.assertEqual(pts[0], [20.0, 10.0])
        self.assertEqual(pts[-1], [20.0, 20.0])
        mid = min(pts, key=lambda p: abs(p[0] - 25.0))
        self.assertAlmostEqual(mid[0], 25.0, places=2)

    def test_65_step_unsupported_warning(self):
        """STEP 遇到 NURBS/B-rep 必须显式告警，不能静默忽略。"""
        from aepcb import step
        src = step.make_fixture_step(60, 40, 1.2)
        src = src.replace(
            "ENDSEC;\nEND-ISO",
            "#900=B_SPLINE_SURFACE_WITH_KNOTS('',3,3,((0.,45.,90.)),.UNSPECIFIED.,"
            ".F.,.F.,(4,4),(4,4),(0.,0.,1.),(0.,0.,1.),.UNSPECIFIED.);\n"
            "#901=ADVANCED_FACE('',(#902),#903,.T.);\nENDSEC;\nEND-ISO")
        m = step.parse(src)
        codes = [c for c, _, _ in m.diagnostics]
        self.assertIn("STEP-UNSUPPORTED-GEOMETRY", codes, "未支持的几何必须告警")
        self.assertIn("B_SPLINE_SURFACE_WITH_KNOTS", m.unsupported)
        self.assertIn("ADVANCED_FACE", m.unsupported)
        # 普通文件不得误报
        clean = step.parse(step.make_fixture_step(60, 40, 1.2))
        self.assertEqual(clean.unsupported, {})
        self.assertNotIn("STEP-UNSUPPORTED-GEOMETRY", [c for c, _, _ in clean.diagnostics])

    def test_66_polygon_pad(self):
        """多边形/自定义焊盘：映射到 other，按外接矩形近似并留告警。"""
        from aepcb import kicad
        text = '''(kicad_pcb (version 20240108)
          (net 0 "") (net 2 "SIG")
          (footprint "BGA" (layer "F.Cu") (at 10 10)
            (fp_text reference "U1" (at 0 0))
            (pad "A1" smd circle (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "SIG"))
            (pad "A2" smd oval (at 1 0) (size 0.8 0.5) (layers "F.Cu") (net 2 "SIG"))
            (pad "A3" smd custom (at 2 0) (size 1 1) (layers "F.Cu") (net 2 "SIG")
              (primitives (gr_poly (pts (xy -0.5 -0.5) (xy 0.5 -0.5)
                (xy 0.5 0.5) (xy -0.5 0.5)))))))
        '''
        spec = kicad.parse(text)
        shapes = {p["no"]: p["shape"] for p in spec["pads"]}
        self.assertEqual(shapes["A1"], "circle")
        self.assertEqual(shapes["A2"], "roundrect")
        self.assertEqual(shapes["A3"], "other")
        custom = [p for p in spec["pads"] if p["no"] == "A3"][0]
        self.assertEqual(len(custom["polygon"]), 4, "多边形顶点应被保留")
        self.assertAlmostEqual(custom["w"], 1.0, places=3)
        self.assertAlmostEqual(custom["h"], 1.0, places=3)
        self.assertTrue(any(w.get("shape") == "custom" for w in spec["pad_warnings"]),
                        "变形焊盘必须告警，不能静默当矩形")


if __name__ == "__main__":
    unittest.main(verbosity=2)
