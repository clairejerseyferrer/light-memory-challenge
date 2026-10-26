"""Initial rule-based IS logic. Thresholds are provisional and require team approval."""
def analyze_performance(accuracy, mistakes, avg_response_time_ms, round_data=None):
    round_data = round_data or []
    if accuracy >= 90 and avg_response_time_ms is not None and avg_response_time_ms <= 1500:
        classification = "Excellent"
    elif accuracy >= 80:
        classification = "Good"
    elif accuracy >= 60:
        classification = "Average"
    else:
        classification = "Struggling"
    adjustment = {"Excellent": 2, "Good": 1, "Average": 0, "Struggling": -1}[classification]
    recommendations = []
    if accuracy < 60:
        recommendations.append("Practice memorizing the full sequence before responding.")
    elif accuracy < 80:
        recommendations.append("Focus on accuracy before trying to increase speed.")
    else:
        recommendations.append("Keep practicing while maintaining your current accuracy.")
    if avg_response_time_ms is not None and avg_response_time_ms > 2200:
        recommendations.append("Work on recalling the sequence more quickly without sacrificing accuracy.")
    if mistakes >= 3:
        recommendations.append("Review rounds where mistakes occurred and practice those patterns.")
    counts = {}
    for item in round_data:
        buttons = item.get("incorrect_buttons", [])
        if isinstance(buttons, list):
            for button in buttons:
                if isinstance(button, str): counts[button] = counts.get(button, 0) + 1
    if counts:
        missed = max(counts, key=counts.get)
        recommendations.append(f"Pay extra attention to button {missed}, which appeared most often in recorded mistakes.")
    return {"classification": classification, "proposed_level_adjustment": adjustment, "recommendations": recommendations, "rules_version": "provisional-1"}
