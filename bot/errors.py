from __future__ import annotations

from typing import Sequence


class UserFacingError(Exception):
    """메시지를 디스코드 유저에게 그대로 보여 줘도 되는 오류. 나머지 인자는 안내 카드(views.base.notice) 모양을 정한다."""

    def __init__(
        self,
        message: str,
        *,
        title: str = "요청을 처리하지 못했어요",
        tone: str = "error",
        facts: Sequence[tuple[str, str]] = (),
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.title = title
        self.tone = tone
        self.facts = tuple(facts)
        self.hint = hint


class PriceUnavailableError(UserFacingError):
    def __init__(self) -> None:
        super().__init__(
            "업비트·바이낸스 시세를 받아오지 못했어요. 잠시 후 다시 시도해 주세요.",
            title="시세를 불러오지 못했어요",
        )


class GlobalLimitError(UserFacingError):
    # 전체 한도(GLOBAL_DAILY_LIMIT_KRW)에 걸린 경우. spent와 limit은 관리자 알림용이고 유저에게는 보여 주지 않는다
    def __init__(self, spent: int, limit: int) -> None:
        super().__init__(
            "서비스 전체의 24시간 송금 한도에 도달해서 잠시 송금을 멈췄어요.",
            title="지금은 송금할 수 없어요",
            tone="warning",
            hint="시간이 지나면 다시 보낼 수 있어요 · 관리자에게도 자동으로 알렸어요",
        )
        self.spent = spent
        self.limit = limit


class RejectedLimitError(UserFacingError):
    # 최근 window_minutes분 동안 바이낸스가 이 유저의 출금을 count번 거절한 경우 (MAX_REJECTED_TRANSFERS)
    def __init__(self, count: int, limit: int, window_minutes: int) -> None:
        super().__init__(
            "거래소가 최근 출금을 여러 번 거절해서 잠시 송금을 멈췄어요.",
            title="잠시 후 다시 송금해 주세요",
            tone="warning",
            facts=[(f"최근 {window_minutes}분 거절", f"{count}번")],
            hint=f"받는 주소·네트워크·메모를 다시 확인해 주세요 · {window_minutes}분 안에 "
            f"{limit}번 거절되면 잠시 멈춰요 · 관리자에게도 자동으로 알렸어요",
        )
        self.count = count
        self.limit = limit
        self.window_minutes = window_minutes


class ServicePausedError(UserFacingError):
    # 관리자가 멈췄거나 지급정지 문자가 온 경우 (Database.pause_service)
    def __init__(self) -> None:
        super().__init__(
            "확인할 일이 생겨 충전과 송금을 잠시 멈췄어요.",
            title="지금은 충전·송금을 쉬고 있어요",
            tone="warning",
            hint="잔액은 그대로 보관돼요 · 다시 열리면 이용할 수 있어요 · 지금은 입금하지 마세요",
        )


class TransferPendingError(UserFacingError):
    # 출금 결과를 아직 모르는 상태. reconcile이 나중에 정리한다
    def __init__(self) -> None:
        super().__init__(
            "바이낸스에서 출금 결과를 바로 받지 못해 자동으로 확인하고 있어요.",
            title="출금 결과를 확인하고 있어요",
            tone="warning",
            hint="출금되지 않은 것으로 확인되면 잔액을 환불하고 DM으로 알려 드려요",
        )
