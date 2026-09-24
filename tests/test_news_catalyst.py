from app.services.news_context import _classify_catalyst


def test_news_classifier_structures_material_events_without_claiming_verification():
    event = _classify_catalyst("Exchange announces token listing next week", "Example News")
    assert event["event_type"] == "LISTING"
    assert event["estimated_magnitude"] == "HIGH"
    assert event["official_source_verified"] is False
    assert event["requires_primary_source_verification"] is True


def test_hack_event_is_negative_high_magnitude_context_not_trade_signal():
    event = _classify_catalyst("Protocol hacked after bridge exploit", "Example News")
    assert event["event_type"] == "HACK_EXPLOIT"
    assert event["direction_hint"] == "NEGATIVE"
    assert event["estimated_magnitude"] == "HIGH"
