"""Tests for the read-only `pending` and `resume` projections (docs/PENDING.md)."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ai_comm.py"
sys.path.insert(0, str(SCRIPT.parent))
import ai_comm as ac  # noqa: E402


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-comm-pending-")
        self.path = Path(self.temp.name) / "channel"
        ac.Channel.init(self.path, "Review a generic change", ["alpha", "beta"])
        self.channel = ac.Channel(self.path)
        self.channel.join("alpha", "host-a", "alpha-session")
        self.channel.join("beta", "host-b", "beta-session")
        self.keys = 0

    def tearDown(self):
        self.temp.cleanup()

    def send(self, author="alpha", act="FACT", **extra):
        self.keys += 1
        to = ["beta"] if author == "alpha" else ["alpha"]
        draft = dict({"act": act, "to": to, "topic": "review", "summary": "A generic message."}, **extra)
        return self.channel.send(author, author + "-session", f"k{self.keys}", draft)["message"]

    def artifact(self, text):
        return self.channel.put_artifact("alpha", "alpha-session", text.encode())["artifact"]

    def propose(self, text="change r1", **extra):
        digest = self.artifact(text)
        return self.send(act="PROPOSE", artifact=digest, **extra), digest

    def stance(self, author, act, proposal, digest):
        return self.send(author=author, act=act, reply_to=proposal["id"], artifact=digest)

    def items(self, handle=None, unclassified=False):
        return self.channel.snapshot().pending(handle, unclassified=unclassified)["items"]

    def actions(self, handle=None, unclassified=False):
        return sorted((e["id"], e["owner"], e["action"]) for e in self.items(handle, unclassified))

    def cli(self, *args):
        return subprocess.run([sys.executable, "-I", str(SCRIPT), "--channel", str(self.path), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    def tree_digest(self):
        digest = hashlib.sha256()
        for root, dirs, files in sorted(os.walk(self.path)):
            dirs.sort()
            digest.update(("dir:" + str(Path(root).relative_to(self.path))).encode())
            for name in sorted(files):
                full = Path(root) / name
                digest.update(str(full.relative_to(self.path)).encode())
                digest.update(full.read_bytes())
        return digest.hexdigest()

    # Proposals

    def test_open_proposal_owes_a_stance_from_every_reviewer(self):
        proposal, _ = self.propose()
        self.assertEqual([(proposal["id"], "alpha", "record_stance"), (proposal["id"], "beta", "record_stance")],
                         self.actions())

    def test_one_accept_closes_only_that_reviewers_item(self):
        proposal, digest = self.propose()
        self.stance("beta", "ACCEPT", proposal, digest)
        self.assertEqual([(proposal["id"], "alpha", "record_stance")], self.actions())
        entry = self.items()[0]
        self.assertEqual({"alpha": None, "beta": "ACCEPT"}, entry["stances"])
        self.assertEqual(digest, entry["artifact"])

    def test_all_accepts_leave_the_author_owing_decide_and_decide_closes(self):
        proposal, digest = self.propose()
        self.stance("beta", "ACCEPT", proposal, digest)
        self.stance("alpha", "ACCEPT", proposal, digest)
        self.assertEqual([(proposal["id"], "alpha", "decide")], self.actions())
        self.send(act="DECIDE", reply_to=proposal["id"], artifact=digest, confidence="verified")
        self.assertEqual([], self.actions())

    def test_reject_leaves_the_author_owing_a_revision(self):
        proposal, digest = self.propose()
        self.stance("beta", "REJECT", proposal, digest)
        self.assertIn((proposal["id"], "alpha", "revise_or_withdraw"), self.actions())

    def test_a_review_alone_closes_nothing(self):
        proposal, _ = self.propose()
        self.send(author="beta", act="REVIEW", reply_to=proposal["id"], body="Please narrow section 2.")
        self.assertEqual(2, len(self.actions()))

    def test_explicit_supersession_closes_the_older_proposal(self):
        old, _ = self.propose("r1")
        new, _ = self.propose("r2", body=f"supersedes: {old['id']}\nr2 text",
                              sources=[{"id": "old-proposal", "kind": "conversation", "ref": old["id"],
                                        "detail": "r1"}])
        self.assertEqual({new["id"]}, {e["id"] for e in self.items()})
        self.assertEqual([], self.items(unclassified=True))

    def test_supersession_through_a_previously_declared_source_reference(self):
        old, _ = self.propose("r1", sources=[{"id": "r1-self", "kind": "conversation", "ref": "placeholder",
                                              "detail": "unrelated"}])
        self.send(body="note", sources=[{"id": "r1-ref", "kind": "conversation", "ref": old["id"],
                                         "detail": "r1"}])
        new, _ = self.propose("r2", body=f"supersedes: {old['id']}", refs=["r1-ref"])
        self.assertEqual({new["id"]}, {e["id"] for e in self.items()})

    def test_supersession_needs_both_the_body_line_and_the_source(self):
        old, _ = self.propose("r1")
        self.propose("r2", body=f"supersedes: {old['id']}")
        self.assertIn(old["id"], {e["id"] for e in self.items()})

    def test_supersession_by_another_author_does_not_close(self):
        old, _ = self.propose("r1")
        digest = self.artifact("beta r2")
        self.send(author="beta", act="PROPOSE", artifact=digest, topic="other",
                  body=f"supersedes: {old['id']}",
                  sources=[{"id": "old", "kind": "conversation", "ref": old["id"], "detail": "r1"}])
        self.assertIn((old["id"], "beta", "record_stance"), self.actions())

    def test_a_newer_proposal_without_the_convention_leaves_the_older_one_open(self):
        old, _ = self.propose("r1")
        new, _ = self.propose("r2")
        self.assertEqual({old["id"], new["id"]}, {e["id"] for e in self.items()})
        self.assertEqual([], self.items(unclassified=True))

    def question_on_proposal(self):
        proposal, digest = self.propose(questions=[{"op": "open", "id": "q-prop", "text": "Which option?"}])
        return proposal, digest

    def resume_view(self, handle):
        result = self.channel.resume(handle, handle + "-session")
        return ([(e["id"], e["action"]) for e in result["pending"]],
                [p["id"] for p in result["open_proposals"]], result["open_proposals_count"])

    def test_question_on_a_decided_proposal_stays_open_until_closed(self):
        proposal, digest = self.question_on_proposal()
        self.assertEqual(([(proposal["id"], "answer_question"), (proposal["id"], "record_stance")],
                          [proposal["id"]], 1), self.resume_view("beta"))
        self.stance("beta", "ACCEPT", proposal, digest)
        self.stance("alpha", "ACCEPT", proposal, digest)
        self.send(act="DECIDE", reply_to=proposal["id"], artifact=digest, confidence="verified")
        self.assertEqual([(proposal["id"], "beta", "answer_question")], self.actions())
        self.assertEqual(([(proposal["id"], "answer_question")], [], 0), self.resume_view("beta"))
        self.assertEqual(([], [], 0), self.resume_view("alpha"))
        self.send(author="beta", questions=[{"op": "close", "id": "q-prop"}])
        self.assertEqual([], self.actions())
        self.assertEqual(([], [], 0), self.resume_view("beta"))

    def test_question_on_a_superseded_proposal_stays_open_until_closed(self):
        proposal, _ = self.question_on_proposal()
        new, _ = self.propose("r2", body=f"supersedes: {proposal['id']}",
                              sources=[{"id": "old", "kind": "conversation", "ref": proposal["id"], "detail": "r1"}])
        self.assertIn((proposal["id"], "beta", "answer_question"), self.actions())
        self.assertNotIn((proposal["id"], "beta", "record_stance"), self.actions())
        pending, open_ids, count = self.resume_view("beta")
        self.assertIn((proposal["id"], "answer_question"), pending)
        self.assertEqual(([new["id"]], 1), (open_ids, count))
        self.send(author="beta", questions=[{"op": "close", "id": "q-prop"}])
        self.assertNotIn((proposal["id"], "answer_question"), self.resume_view("beta")[0])

    def test_cli_resume_with_a_question_on_a_proposal(self):
        proposal, _ = self.question_on_proposal()
        result = self.cli("resume", "--as", "beta", "--session", "beta-session")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, json.loads(result.stdout)["open_proposals_count"])

    # Questions and control exchanges

    def test_structured_question_owed_by_addressee_until_closed(self):
        ask = self.send(act="ASK", questions=[{"op": "open", "id": "q-one", "text": "Which option?"}])
        self.assertEqual([(ask["id"], "beta", "answer_question")], self.actions())
        self.send(author="beta", reply_to=ask["id"], body="An answer without closing.")
        self.assertEqual(1, len(self.actions()), "a reply alone must not close a question")
        self.send(author="beta", reply_to=ask["id"], questions=[{"op": "close", "id": "q-one"}])
        self.assertEqual([], self.actions())

    def test_final_ack_is_owed_only_by_the_ping_sender(self):
        self.channel.join("gamma", "host-c", "gamma-session")
        ping = self.channel.send("alpha", "alpha-session", "p1",
                                 {"act": "PING", "to": ["beta"], "topic": "review", "summary": "ping"})["message"]
        pong = self.channel.send("beta", "beta-session", "p2",
                                 {"act": "PONG", "to": ["alpha", "gamma"], "topic": "review", "summary": "pong",
                                  "reply_to": ping["id"]})["message"]
        self.assertEqual([(pong["id"], "alpha", "final_ack")], self.actions())
        self.channel.send("gamma", "gamma-session", "p3",
                          {"act": "ACK", "to": ["beta"], "topic": "review", "summary": "ack", "reply_to": pong["id"]})
        self.assertEqual([(pong["id"], "alpha", "final_ack")], self.actions(), "gamma's ACK must not close it")
        self.channel.send("alpha", "alpha-session", "p4",
                          {"act": "ACK", "to": ["beta"], "topic": "review", "summary": "ack", "reply_to": pong["id"]})
        self.assertEqual([], self.actions())

    def test_ping_pong_and_final_ack(self):
        ping = self.send(act="PING")
        self.assertEqual([(ping["id"], "beta", "pong")], self.actions())
        pong = self.send(author="beta", act="PONG", reply_to=ping["id"])
        self.assertEqual([(pong["id"], "alpha", "final_ack")], self.actions())
        self.send(reply_to=pong["id"], body="Not the final ACK.")
        self.assertEqual([(pong["id"], "alpha", "final_ack")], self.actions())
        self.send(act="ACK", reply_to=pong["id"])
        self.assertEqual([], self.actions())

    def candidates(self):
        return sorted((e["id"], e["owner"]) for e in self.items(unclassified=True))

    def test_unstructured_messages_are_candidates_until_their_addressee_closes_them(self):
        ask = self.send(act="ASK", body="Can you check this?")
        self.assertEqual([], self.actions())
        self.assertEqual([(ask["id"], "beta", "may_contain_untracked_request")], self.actions(unclassified=True))
        reply = self.send(author="beta", reply_to=ask["id"], body="Looked at it.")
        self.assertIn((ask["id"], "beta"), self.candidates(), "a generic reply must not close it")
        self.channel.inbox("beta", "beta-session")
        self.channel.ack("beta", "beta-session", reply["seq"])
        self.assertIn((ask["id"], "beta"), self.candidates(), "fetch and read must not close it")
        closing = [{"id": "ask-ref", "kind": "conversation", "ref": ask["id"], "detail": "the ask"}]
        wrong = self.send(body=f"closes: {ask['id']}", sources=closing)
        self.assertIn((ask["id"], "beta"), self.candidates(), "only the addressee may close it")
        self.assertIn((wrong["id"], "beta"), self.candidates(), "a no-op closure record stays a candidate")
        closer = self.send(author="beta", body=f"closes: {ask['id']}", refs=["ask-ref"])
        self.assertNotIn((ask["id"], "beta"), self.candidates())
        self.assertNotIn(closer["id"], {c[0] for c in self.candidates()}, "a closure record is not a request")
        self.assertIn((reply["id"], "alpha"), self.candidates(), "closing one item leaves the others")

    def test_fact_correct_and_review_are_candidates_but_agreement_acts_are_not(self):
        proposal, digest = self.propose()
        fact = self.send(body="Q6: what can your host signal?")
        review = self.send(author="beta", act="REVIEW", reply_to=proposal["id"], body="Answers Q6 too.")
        correct = self.send(act="CORRECT", reply_to=fact["id"], confidence="verified", body="Fixing a claim.")
        accept = self.stance("beta", "ACCEPT", proposal, digest)
        self.assertEqual({(fact["id"], "beta"), (review["id"], "alpha"), (correct["id"], "beta")},
                         set(self.candidates()))
        self.assertNotIn(accept["id"], {c[0] for c in self.candidates()})
        self.assertIn((fact["id"], "beta"), self.candidates(), "an indirect answer does not close it")

    def test_invalid_closure_records_remain_candidates(self):
        real = self.send(body="A request?")
        unknown = self.send(body="closes: 00009999-alpha\nalso do X",
                            sources=[{"id": "u1", "kind": "conversation", "ref": "00009999-alpha", "detail": "d"}])
        own = self.send(body=f"closes: {real['id']}\ndo Y",
                        sources=[{"id": "w1", "kind": "conversation", "ref": real["id"], "detail": "d"}])
        fact = self.send(body=f"supersedes: {real['id']}\ndo Z", refs=["w1"])
        candidates = {c[0] for c in self.candidates()}
        for message in (real, unknown, own, fact):
            self.assertIn(message["id"], candidates)

    def test_a_closure_naming_a_decided_proposal_is_not_valid(self):
        proposal, _ = self.propose()
        closer = self.send(author="beta", body=f"closes: {proposal['id']}",
                           sources=[{"id": "p1", "kind": "conversation", "ref": proposal["id"], "detail": "d"}])
        self.assertIn((closer["id"], "alpha"), self.candidates())
        self.assertEqual(2, len(self.actions()), "closes: never closes a proposal")

    def test_structured_questions_stay_out_of_the_candidate_bucket(self):
        ask = self.send(act="ASK", questions=[{"op": "open", "id": "q-s", "text": "Which?"}])
        self.assertEqual([(ask["id"], "beta", "answer_question")], self.actions())
        self.assertEqual([], self.candidates())

    # Filters, paging, crash points

    def test_for_filter_paging_and_counts(self):
        for n in range(3):
            self.send(act="ASK", questions=[{"op": "open", "id": f"q{n}", "text": "Which?"}])
        self.send(act="ASK", body="loose")
        page = self.channel.snapshot().pending("beta", offset=1, limit=1)
        self.assertEqual((3, 1, 2, True, 1), (page["count"], len(page["items"]), page["next"], page["more"],
                                               page["unclassified_count"]))
        self.assertEqual([], self.items("alpha"))

    def test_unknown_participant_is_refused(self):
        with self.assertRaises(ac.ProtocolError) as caught:
            self.channel.snapshot().pending("gamma")
        self.assertEqual("unknown_participant", caught.exception.details["code"])

    def test_work_survives_fetch_read_and_progress_reply(self):
        ask = self.send(act="ASK", questions=[{"op": "open", "id": "q-crash", "text": "Run it?"}])
        self.channel.inbox("beta", "beta-session")
        self.assertEqual(1, len(self.items("beta")), "after fetch")
        self.channel.ack("beta", "beta-session", ask["seq"])
        self.assertEqual(1, len(self.items("beta")), "after read-ack, before work")
        self.send(author="beta", reply_to=ask["id"], body="Started; results later.")
        self.assertEqual(1, len(self.items("beta")), "after a progress reply")
        resumed = self.channel.resume("beta", "beta-session")
        self.assertEqual([ask["id"]], [e["id"] for e in resumed["pending"]])
        self.assertEqual(0, resumed["you"]["unread_addressed"])

    # resume

    def test_resume_lists_mine_waiting_and_open_proposals(self):
        proposal, digest = self.propose()
        self.stance("beta", "ACCEPT", proposal, digest)
        ask = self.send(author="beta", act="ASK", questions=[{"op": "open", "id": "q-r", "text": "Ready?"}])
        result = self.channel.resume("alpha", "alpha-session")
        self.assertEqual({(proposal["id"], "record_stance"), (ask["id"], "answer_question")},
                         {(e["id"], e["action"]) for e in result["pending"]})
        self.assertEqual(0, result["waiting_on_others_count"])
        self.assertEqual([{"id": proposal["id"], "topic": "review", "artifact": digest,
                           "stances": {"alpha": None, "beta": "ACCEPT"}}], result["open_proposals"])
        self.assertEqual(2, result["you"]["unread_addressed"])
        beta = self.channel.resume("beta", "beta-session")
        self.assertEqual([(ask["id"], "alpha", "answer_question")],
                         [(w["id"], w["owner"], w["action"]) for w in beta["waiting_on_others"]])

    def test_resume_refuses_a_stale_session(self):
        with self.assertRaises(ac.ProtocolError) as caught:
            self.channel.resume("alpha", "other-session")
        self.assertEqual("unregistered_or_stale_session", caught.exception.details["code"])

    # Read-only guarantee and CLI

    def test_resume_retries_when_a_receipt_is_newer_than_its_snapshot(self):
        stale = self.channel.snapshot()
        ask = self.send(act="ASK", questions=[{"op": "open", "id": "q-race", "text": "Which?"}])
        self.channel.inbox("beta", "beta-session")
        self.channel.ack("beta", "beta-session", ask["seq"])
        calls = []
        real = self.channel.snapshot

        def racing():
            calls.append(1)
            return stale if len(calls) == 1 else real()
        self.channel.snapshot = racing
        result = self.channel.resume("beta", "beta-session")
        self.assertEqual(2, len(calls))
        self.assertEqual(ask["seq"], result["you"]["read_through"])
        self.assertEqual([ask["id"]], [e["id"] for e in result["pending"]])

    def test_resume_still_reports_a_corrupt_receipt(self):
        receipt = self.path / "ai-comm-runtime" / "beta.json"
        receipt.write_text(json.dumps({"v": 2, "generation": 1, "read": 5, "seen": 2}))
        with self.assertRaises(ac.ProtocolError) as caught:
            self.channel.resume("beta", "beta-session")
        self.assertEqual("corrupt_receipt_bounds", caught.exception.details["code"])

    def test_resume_creates_no_runtime_files(self):
        import shutil
        runtime = self.path / "ai-comm-runtime"
        for remove in ("runtime", "lock"):
            if remove == "runtime":
                shutil.rmtree(runtime, ignore_errors=True)
            elif (runtime / "lock").exists():
                (runtime / "lock").unlink()
            for session in ("other-session", "alpha-session"):
                before = self.tree_digest()
                try:
                    self.channel.resume("alpha", session)
                except ac.ProtocolError:
                    pass
                self.channel.snapshot().pending()
                self.assertEqual(before, self.tree_digest(), f"{remove} absent, session {session}")
            runtime.mkdir(exist_ok=True)

    def test_pending_and_resume_write_nothing(self):
        proposal, _ = self.propose()
        self.send(author="beta", act="ASK", body="loose")
        self.channel.inbox("beta", "beta-session")
        before = self.tree_digest()
        for args in (["pending"], ["pending", "--for", "alpha"], ["pending", "--unclassified"],
                     ["resume", "--as", "alpha", "--session", "alpha-session"]):
            result = self.cli(*args)
            self.assertEqual(0, result.returncode, result.stderr)
            json.loads(result.stdout)
        self.assertEqual(before, self.tree_digest())

    def test_cli_errors_are_json(self):
        result = self.cli("pending", "--for", "gamma")
        self.assertEqual(2, result.returncode)
        self.assertEqual("unknown_participant", json.loads(result.stderr)["code"])
        result = self.cli("resume", "--as", "alpha")
        self.assertEqual("invalid_cli_arguments", json.loads(result.stderr)["code"])


if __name__ == "__main__":
    unittest.main()
