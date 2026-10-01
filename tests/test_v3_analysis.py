import unittest
from copy import deepcopy

from app.analysis import analyze_public_speeches


def speech(seq, pid, text, audience="public"):
    return {"type": "chat_message", "event_id": f"event:{seq}", "audience": audience,
            "data": {"kind": "speech", "day": 1, "player_id": pid, "speech": text}}


class PublicSpeechAnalysis(unittest.TestCase):
    def test_target_changes_have_event_provenance_and_do_not_confirm_claims(self):
        events = [speech(1, 2, "我是预言家，查验12号是狼人。支持10号。"),
                  speech(2, 2, "我是女巫，查验12号不是狼。怀疑10号。")]
        before = deepcopy(events)
        actor = analyze_public_speeches(events)["players"]["2"]
        self.assertEqual(actor["judgment_change_count"], 1)
        self.assertEqual(actor["judgment_changes"][0]["target_player_id"], 10)
        self.assertEqual(actor["judgment_changes"][0]["previous_event_id"], "event:1")
        self.assertEqual({entry["type"] for entry in actor["claim_changes"]}, {"role_claim_change", "check_claim_change"})
        self.assertTrue(all(not claim["confirmed"] for claim in actor["role_claims"] + actor["check_claims"]))
        self.assertEqual(events, before)

    def test_quotes_past_stances_additional_support_and_duplicates_are_not_changes(self):
        events = [speech(1, 2, "支持10号。"), speech(2, 2, "也支持12号。"),
                  speech(3, 2, "3号说怀疑10号，不能当事实。"),
                  speech(4, 2, "我之前支持10号，现在怀疑10号。")]
        events.append(deepcopy(events[3]))
        report = analyze_public_speeches(events)
        actor = report["players"]["2"]
        self.assertEqual(actor["speech_count"], 4)
        self.assertEqual(actor["judgment_change_count"], 1)
        self.assertEqual(len(actor["stance_history"]), 3)
        self.assertEqual(actor["judgment_changes"][0]["event_id"], "event:4")

    def test_negated_check_and_suspicion_do_not_become_positive_claims(self):
        actor = analyze_public_speeches([speech(1, 2, "我怀疑12号。"),
            speech(2, 2, "我不怀疑12号，我没有查验10号。")])["players"]["2"]
        self.assertEqual([item["stance"] for item in actor["stance_history"]], ["suspect", "neutral"])
        self.assertEqual(actor["judgment_change_count"], 1)
        self.assertEqual(actor["check_claims"], [])

    def test_only_public_speech_is_analyzed(self):
        events = [speech(1, 2, "我怀疑12号。", "wolves"), speech(2, 2, "我是预言家。", "player"),
                  {"type": "private_pet_message", "audience": "player", "player_id": 2,
                   "data": {"speech": "私有建议，支持12号。"}},
                  speech(3, 3, "我是骑士，支持12号。")]
        report = analyze_public_speeches(events)
        self.assertNotIn("2", report["players"])
        self.assertEqual(report["players"]["3"]["role_claims"][0]["claimed_role"], "knight")
        self.assertEqual(report["speech_count"], 1)

    def test_withdrawal_records_neutral_instead_of_repeating_positive_stance(self):
        actor = analyze_public_speeches([speech(1, 2, "支持12号。"),
            speech(2, 2, "我不再支持12号。")])["players"]["2"]
        self.assertEqual([item["stance"] for item in actor["stance_history"]], ["support", "neutral"])
        self.assertEqual(actor["judgment_change_count"], 1)


if __name__ == "__main__":
    unittest.main()
