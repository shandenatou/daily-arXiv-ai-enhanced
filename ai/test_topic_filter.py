import unittest

from topic_filter import filter_papers_by_topics, matching_topics, parse_topics


ALL_TOPICS = parse_topics(
    "world_model,vla,embodied,reinforcement_learning"
)


class TopicFilterTests(unittest.TestCase):
    def test_irrelevant_computer_vision_paper_is_removed(self):
        paper = {
            "title": "Convolutional Networks for Facial Expression Recognition",
            "summary": "We compare image classification methods on three datasets.",
            "categories": ["cs.CV"],
        }
        self.assertEqual(matching_topics(paper, ALL_TOPICS), ())

    def test_all_reinforcement_learning_domains_are_kept(self):
        paper = {
            "title": "Deep Reinforcement Learning for Active Trading",
            "summary": "An RL agent learns a policy for financial markets.",
            "categories": ["cs.LG"],
        }
        self.assertIn("reinforcement_learning", matching_topics(paper, ALL_TOPICS))

    def test_all_world_model_domains_are_kept(self):
        paper = {
            "title": "Executable World Models for Coding Agents",
            "summary": "A learned environment model supports planning.",
            "categories": ["cs.AI"],
        }
        self.assertIn("world_model", matching_topics(paper, ALL_TOPICS))

    def test_vla_is_kept(self):
        paper = {
            "title": "A Vision-Language-Action Model for Generalist Control",
            "summary": "The VLA transfers across tasks.",
            "categories": ["cs.RO"],
        }
        self.assertIn("vla", matching_topics(paper, ALL_TOPICS))

    def test_cs_ro_is_kept_as_embodied(self):
        paper = {
            "title": "Learning Contact-Rich Skills",
            "summary": "A new controller is evaluated in simulation.",
            "categories": ["cs.RO"],
        }
        self.assertIn("embodied", matching_topics(paper, ALL_TOPICS))

    def test_empty_configuration_keeps_everything(self):
        papers = [{"id": "1"}, {"id": "2"}]
        filtered, counts = filter_papers_by_topics(papers, "")
        self.assertEqual(filtered, papers)
        self.assertEqual(counts, {})

    def test_short_rl_does_not_match_inside_another_word(self):
        paper = {
            "title": "Natural Language Models",
            "summary": "We study URLs and parallel evaluation.",
            "categories": ["cs.CL"],
        }
        self.assertNotIn(
            "reinforcement_learning", matching_topics(paper, ALL_TOPICS)
        )


if __name__ == "__main__":
    unittest.main()
