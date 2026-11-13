"""Which blocks an update fetches: the tip first, then history newest first, within what the node
still has and the disk allows."""
import os, unittest

import support


def records(ns, heights):
    blocks = os.path.join(ns.home, "blocks")
    os.makedirs(blocks, exist_ok=True)
    for h in heights:
        open(os.path.join(blocks, f"{h}.json"), "w").close()


class PlanTests(unittest.TestCase):
    def plan(self, ns, top, tip, pruneheight=0, **env):
        local = ns.module("local")
        local._run.clear()
        local._info = {"blocks": tip, "pruned": bool(pruneheight), "pruneheight": pruneheight, "bestblockhash": "x"}
        return local, local.heights(top, tip)

    def test_missing_and_holes(self):
        with support.cesspool() as ns:
            local = ns.module("local")
            f = local.FORK
            self.assertEqual(local.missing([f, f + 1, f + 4], f + 5, 10), [f + 5, f + 3, f + 2])
            self.assertEqual(local.missing([f + 3], f + 3, 10), [f + 2, f + 1, f])
            self.assertEqual(local.missing([f + 3], f + 3, 2), [f + 2, f + 1])
            self.assertEqual(local.missing([], f + 1, 10), [f + 1, f])
            self.assertEqual(local.holes([f, f + 1, f + 4, f + 9]), [[f + 2, f + 3], [f + 5, f + 8]])

    def test_first_run_starts_near_the_tip(self):
        with support.cesspool() as ns:
            local = ns.module("local")
            local._info = {"blocks": 981500, "pruned": False}
            self.assertEqual(local.start(981500), 981500 - local.RECENT + 1)
            _, new = self.plan(ns, local.start(981500) - 1, 981500)
            self.assertEqual(new, list(range(981489, 981501)))

    def test_first_run_never_starts_below_the_fork_or_the_prune_height(self):
        with support.cesspool() as ns:
            local = ns.module("local")
            local._info = {"blocks": local.FORK + 3, "pruned": False}
            self.assertEqual(local.start(local.FORK + 3), local.FORK)
            local._info = {"blocks": 981500, "pruned": True, "pruneheight": 981495}
            self.assertEqual(local.start(981500), 981495)

    def test_new_blocks_come_alone(self):
        with support.cesspool() as ns:
            records(ns, range(981490, 981499))
            _, new = self.plan(ns, 981498, 981500)
            self.assertEqual(new, [981499, 981500])  # history waits, though plenty is missing

    def test_history_fills_newest_first_once_the_tip_is_reached(self):
        with support.cesspool() as ns:
            records(ns, range(981490, 981501))
            local, new = self.plan(ns, 981500, 981500)
            self.assertEqual(new[:3], [981489, 981488, 981487])
            self.assertLessEqual(len(new), local.FILL_MOST)
            self.assertEqual(local._run["source"], "node")

    def test_history_stops_at_the_prune_height_without_an_indexer(self):
        with support.cesspool() as ns:
            records(ns, range(981490, 981501))
            _, new = self.plan(ns, 981500, 981500, pruneheight=981487)
            self.assertEqual(new, [981489, 981488, 981487])
            records(ns, [981487, 981488, 981489])
            _, new = self.plan(ns, 981500, 981500, pruneheight=981487)
            self.assertEqual(new, [])

    def test_a_long_stop_shows_the_tip_first(self):
        with support.cesspool() as ns:
            records(ns, range(981000, 981011))
            local, new = self.plan(ns, 981010, 981500)
            self.assertEqual(new, list(range(981500 - local.RECENT + 1, 981501)))

    def test_a_stop_longer_than_the_prune_window_starts_where_the_node_does(self):
        with support.cesspool() as ns:
            records(ns, range(981000, 981011))
            _, new = self.plan(ns, 981010, 981020, pruneheight=981015)
            self.assertEqual(new, list(range(981015, 981021)))

    def test_a_full_disk_stops_everything(self):
        with support.cesspool() as ns:
            local = ns.module("local")
            local.disk_free = lambda: 10
            _, new = self.plan(ns, 981498, 981500)
            self.assertEqual(new, [])
            self.assertEqual(local._run["blocked"], "disk")

    def test_the_ceiling_stops_history_but_not_new_blocks(self):
        with support.cesspool({"CESSPOOL_MAX_GB": "1"}) as ns:
            local = ns.module("local")
            records(ns, range(981490, 981501))
            local._run.clear()
            local._info = {"blocks": 981500, "pruned": False, "bestblockhash": "x"}
            local._run["bytes"] = local.MAX_BYTES  # full
            self.assertEqual(local.heights(981500, 981500), [])
            self.assertEqual(local._run["blocked"], "budget")
            local._run["bytes"] = local.MAX_BYTES
            self.assertEqual(local.heights(981498, 981500), [981499, 981500])

    def test_pages_are_rendered_when_filling_stops_with_records_waiting(self):
        with support.cesspool() as ns:
            local = ns.module("local")
            f = local.FORK
            records(ns, range(f, f + 5))
            os.makedirs(os.path.join(ns.out, "block", str(f + 4)))
            with open(os.path.join(ns.out, "tip.json"), "w") as fh:
                fh.write("{}")
            local._run.clear()
            local._info = {"blocks": f + 4, "pruned": False, "bestblockhash": "x"}
            # Four records have no page, nothing more is coming, and this update fetched nothing.
            self.assertTrue(local.build_due(ns.out, [], f + 4))
            for h in range(f, f + 4):
                os.makedirs(os.path.join(ns.out, "block", str(h)))
            self.assertFalse(local.build_due(ns.out, [], f + 4))   # every record has its page: nothing to do
            self.assertTrue(local.build_due(ns.out, [f + 5], f + 4))  # a new block always renders

    def test_batches_respect_size_and_count(self):
        with support.cesspool() as ns:
            local = ns.module("local")
            raw = os.path.join(ns.home, "raw")
            os.makedirs(raw)
            for h in range(100, 110):
                with open(os.path.join(raw, f"{h}.json.gz"), "wb") as f:
                    f.write(b"x" * (local.BATCH_BYTES // 3))
            runs = list(local.batches(list(range(100, 110))))
            self.assertEqual([len(r) for r in runs], [3, 3, 3, 1])
            self.assertEqual(sum(runs, []), list(range(100, 110)))

    def test_coverage_words(self):
        with support.cesspool() as ns:
            local = ns.module("local")
            self.assertEqual(local.since(), "since the fork")
            local.begin_build({})  # nothing graded: the fork stands
            self.assertEqual(local.first(), local.FORK)
            local._built.update(start=975000, top=975100, gaps=[], blocks=101, cov={})
            self.assertEqual(local.since(), "since block 975000")
            self.assertEqual(local.since_title(), "Since block 975000")
            self.assertEqual(local.from_the_fork(), "from 975000 on")
            self.assertIn('data-fork="975000"', local.body_attrs())

    def test_a_release_that_grades_differently_queues_the_stored_blocks(self):
        with support.cesspool() as ns:
            local = ns.module("local")
            f = local.FORK
            local.prepare()                       # a first run: nothing on file, nothing queued
            self.assertEqual(local._regrade()["left"], [])
            records(ns, [f, f + 1, f + 2, f + 7, f + 8])
            local.prepare()                       # same grader: still nothing
            self.assertEqual(local._regrade()["left"], [])
            local._grader = "another-release"
            local.prepare()
            self.assertEqual(local._regrade()["left"], [[f, f + 2], [f + 7, f + 8]])
            local._run.clear()
            local._info = {"blocks": f + 8, "pruned": False, "bestblockhash": "x"}
            os.remove(os.path.join(ns.home, "blocks", f"{f + 7}.json"))   # dropped meanwhile
            batch = local.heights(f + 8, f + 8)
            self.assertEqual(batch, [f + 8, f + 2, f + 1, f])              # newest first, the dropped one skipped
            self.assertEqual(local._run["source"], "regrade")
            local.regrade_done({f + 8, f + 2})
            self.assertEqual(local._regrade()["left"], [[f, f + 1]])
            # A new block still comes first.
            self.assertEqual(local.heights(f + 8, f + 9), [f + 9])
            local.regrade_done({f, f + 1})
            self.assertEqual(local._regrade(), {"grader": "another-release", "left": []})

    def test_pool_list_is_this_chains_and_is_never_stale(self):
        for chain, name in (("blake2b", "blake2b.json"), ("sha256", "sha256.json")):
            with support.cesspool(chain=chain) as ns:
                local = ns.module("local")
                local.prepare()
                with open(os.path.join(ns.home, "pools-v2.json"), "rb") as got, open(os.path.join(support.ROOT, "docker", "pools", name), "rb") as want:
                    self.assertEqual(got.read(), want.read())
                os.utime(os.path.join(ns.home, "pools-v2.json"), (0, 0))  # as if a week had passed
                local.prepare()
                self.assertGreater(os.path.getmtime(os.path.join(ns.home, "pools-v2.json")), 1e9)
                self.assertEqual(os.environ["PLUMB_CHECK_CHAIN"], chain)


if __name__ == "__main__":
    unittest.main()
