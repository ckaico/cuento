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
