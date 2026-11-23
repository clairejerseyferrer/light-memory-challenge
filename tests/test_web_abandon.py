"""Regression checks for website session abandonment and polling safety."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = (ROOT / "web/index.html").read_text(encoding="utf-8")

class WebAbandonRegressionTests(unittest.TestCase):
    def test_abandon_stops_polling_before_request(self):
        handler = WEB.split('$("abandonButton").onclick = async () => {', 1)[1].split('};', 1)[0]
        self.assertIn("isAbandoning = true", handler)
        self.assertIn("stopTimers();", handler)
        self.assertIn("/abandon", handler)

    def test_poll_responses_are_invalidated_when_session_or_generation_changes(self):
        poller = WEB.split("async function pollSession() {", 1)[1].split("function startPolling()", 1)[0]
        self.assertIn("requestedSessionId = sid", poller)
        self.assertIn("requestedGeneration = pollGeneration", poller)
        self.assertIn("pollGeneration !== requestedGeneration", poller)
        self.assertIn("isAbandoning", poller)

    def test_stop_timers_invalidates_in_flight_poll(self):
        stop = WEB.split("function stopTimers() {", 1)[1].split("function tab(", 1)[0]
        self.assertIn("pollGeneration++", stop)

if __name__ == "__main__":
    unittest.main()
