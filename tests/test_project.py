import copy
import os
import tempfile
import unittest

from cuento import project as P


def doc():
    d = P.empty_doc("t")
    d["sources"]["A"] = {"proxy": "/x", "frames": 1000}
    for i, (a, b) in enumerate([(100, 200), (300, 350), (500, 700)]):
        d["seq"].append({"id": f"b{i}", "type": "block", "source": "A", "in": a, "out": b, "text": f"t{i}", "audio": "lav"})
    P.normalize(d)
    return d


def run(d, op, **args):
    r = P.OPS[op](d, **args)
    P.normalize(d)
    return r


def starts(d):
    return [(it["id"], s, n) for it, s, n in P.layout(d)[0]]


class Ops(unittest.TestCase):
    def test_trim_ripples(self):
        d = doc()
        run(d, "clip_trim", id="b0", out=150)
        self.assertEqual(starts(d), [("b0", 0, 50), ("b1", 50, 50), ("b2", 100, 200)])
        with self.assertRaises(P.OpError):
            run(d, "clip_trim", id="b0", out=5000)

    def test_split_then_q_w(self):
        d = doc()
        r = run(d, "clip_split", frame=40)
        self.assertEqual(len(d["seq"]), 4)
        self.assertEqual((d["seq"][0]["out"], d["seq"][1]["in"]), (140, 140))
        self.assertIsNone(run(d, "clip_split", frame=40)["split"])
        d = doc()
        self.assertEqual(run(d, "ripple_trim_to", frame=120, side="prev")["playhead"], 100)
        self.assertEqual(d["seq"][1]["in"], 320)
        run(d, "ripple_trim_to", frame=110, side="next")
        self.assertEqual(d["seq"][1]["out"], 330)

    def test_lift_and_ripple_delete(self):
        d = doc()
        run(d, "clip_delete", ids=["b1"])
        self.assertEqual([it["type"] for it in d["seq"]], ["block", "gap", "block"])
        self.assertEqual(P.layout(d)[1], 350)
        run(d, "range_delete", start=50, end=160, ripple=True)
        self.assertEqual(P.layout(d)[1], 240)

    def test_roll_slip_slide_keep_length(self):
        d = doc()
        run(d, "clip_roll", id="b0", delta=10)
        self.assertEqual(starts(d), [("b0", 0, 110), ("b1", 110, 40), ("b2", 150, 200)])
        self.assertEqual((d["seq"][0]["out"], d["seq"][1]["in"]), (210, 310))
        run(d, "clip_slip", id="b1", delta=-20)
        self.assertEqual((d["seq"][1]["in"], d["seq"][1]["out"]), (290, 330))
        self.assertEqual(P.layout(d)[1], 350)
        run(d, "clip_slide", id="b1", delta=-30)
        self.assertEqual(starts(d), [("b0", 0, 80), ("b1", 80, 40), ("b2", 120, 230)])
        self.assertEqual((d["seq"][1]["in"], d["seq"][2]["in"]), (290, 470))
        with self.assertRaises(P.OpError):
            run(d, "clip_slip", id="b1", delta=-1000)
        with self.assertRaises(P.OpError):
            run(d, "clip_roll", id="b2", delta=5)
        with self.assertRaises(P.OpError):
            run(d, "clip_slide", id="b0", delta=5)

    def test_roll_keeps_comment_in_place(self):
        d = doc()
        run(d, "comment_add", start=160, dur=5, text="x")
        run(d, "clip_roll", id="b0", delta=-20)
        self.assertEqual(d["comments"][0]["start"], 160)

    def test_comment_rides_its_clip(self):
        d = doc()
        run(d, "comment_add", start=160, dur=20, text="here")
        run(d, "clip_trim", id="b0", out=150)          # upstream ripple: -50
        self.assertEqual(d["comments"][0]["start"], 110)
        run(d, "clip_trim", id="b2", **{"in": 505})    # head trim keeps the same source frame
        self.assertEqual(d["comments"][0]["start"], 105)
        run(d, "clip_move", id="b2", index=0)
        self.assertEqual(d["comments"][0]["start"], 5)

    def test_comment_edit_reopens(self):
        d = doc()
        cid = run(d, "comment_add", start=0, text="scroll")["id"]
        lid = run(d, "layer_create", source="<b>x</b>", start=0, dur=48, comment_id=cid)["id"]
        run(d, "comment_resolve", id=cid, layer_id=lid)
        self.assertEqual(d["comments"][0]["status"], "resolved")
        run(d, "comment_edit", id=cid, text="bigger scroll")
        self.assertEqual((d["comments"][0]["status"], d["comments"][0]["layer_id"]), ("open", lid))

    def test_layer_hash_ignores_position(self):
        d = doc()
        lid = run(d, "layer_create", source="<b>x</b>", start=0, dur=48)["id"]
        h = P.layer_hash(d["layers"][0])
        run(d, "layer_update", id=lid, start=30, z="behind_person")
        self.assertEqual(P.layer_hash(d["layers"][0]), h)
        run(d, "layer_update", id=lid, source="<b>y</b>")
        self.assertNotEqual(P.layer_hash(d["layers"][0]), h)


class Versions(unittest.TestCase):
    def test_switch_keeps_each_cut_whole(self):
        d = doc()
        run(d, "comment_add", start=10, text="on A")
        run(d, "version_add", name="B", seq=[{"source": "A", "in": 100, "out": 200}, {"source": "A", "in": 800, "out": 900}])
        run(d, "version_switch", name="B")
        self.assertEqual((d["version"], len(d["seq"]), d["comments"]), ("B", 2, []))
        self.assertEqual(len(d["versions"]["A"]["seq"]), 3)
        run(d, "clip_trim", id=d["seq"][0]["id"], out=150)
        run(d, "version_switch", name="A")
        self.assertEqual((len(d["seq"]), d["comments"][0]["text"]), (3, "on A"))
        self.assertEqual(d["versions"]["B"]["seq"][0]["out"], 150)

    def test_diff_is_by_source_footage(self):
        d = doc()
        run(d, "version_add", name="B", seq=[{"source": "A", "in": 100, "out": 180}, {"source": "A", "in": 800, "out": 900}])
        x = P.version_diff(d, "A", "B")
        self.assertEqual(x["A"]["frames_only_here"], 20 + 50 + 200)
        self.assertEqual([c["whole_clip"] for c in x["A"]["clips_only_here"]], [False, True, True])
        self.assertEqual(x["B"]["frames_only_here"], 100)
        with self.assertRaises(P.OpError):
            run(d, "version_delete", name="A")


class History(unittest.TestCase):
    def test_undo_redo_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            P.HOME, P.REGISTRY, P.CACHE = tmp, os.path.join(tmp, "projects.json"), os.path.join(tmp, "cache")
            p = P.Project.create("t", os.path.join(tmp, "proj"), doc())
            before = copy.deepcopy(p.doc)
            p.apply("clip_trim", {"id": "b0", "out": 150})
            p.apply("clip_split", {"frame": 10})
            self.assertEqual(p.rev, 3)
            p.undo(); p.undo()
            self.assertEqual(p.doc, before)
            self.assertFalse(p.undo()["ok"])
            p.redo()
            again = P.Project("t", os.path.join(tmp, "proj"))
            self.assertEqual(again.doc["seq"][0]["out"], 150)
            self.assertTrue(again.state()["can_redo"])
            again.apply("clip_trim", {"id": "b0", "out": 160})
            self.assertFalse(again.state()["can_redo"])
            self.assertEqual(len(os.listdir(os.path.join(tmp, "proj", "history"))), 4)


if __name__ == "__main__":
    unittest.main()
