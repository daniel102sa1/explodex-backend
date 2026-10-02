from app.practice_routes import PracticeModifyRequest
from app.services.practice_trading import _validate_geometry


def test_single_target_request_explicit_and_backward_compatible() -> None:
    legacy = PracticeModifyRequest(session_id="practice-12345", stop_loss=98, take_profit=104)
    assert legacy.single_target is False
    one_tp = PracticeModifyRequest(session_id="practice-12345", stop_loss=98, take_profit=104, single_target=True)
    assert one_tp.single_target is True


def test_one_tp_long_and_short_are_valid() -> None:
    _validate_geometry(side="LONG", entry=100, stop_loss=98, take_profit=104, tp2=None, tp3=None)
    _validate_geometry(side="SHORT", entry=100, stop_loss=102, take_profit=96, tp2=None, tp3=None)
