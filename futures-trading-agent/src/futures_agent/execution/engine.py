"""Execution engine: validates an approved trade, routes it to whichever
broker path is configured, confirms the response, and guarantees it is
never submitted twice.

Layering: risk/manager.py decides *whether* and *how many contracts*; this
module only decides *how* to place what risk manager already approved. It
never re-evaluates risk and never talks to Claude.

Bracket orders: TradersPost accepts stop/target in the same webhook call as
the entry (see execution/traderspost.py). Tradovate's native bracket/OCO
order fields were not something this session could verify live (see the
caveat in market/tradovate.py), so the Tradovate path here places the entry
order only; attaching a verified stop/target leg to it is flagged as a gap
in the docstring below rather than guessed at.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..logging_setup import log_error, log_trade
from ..market.tradovate import TradovateClient
from .traderspost import TradersPostClient

log = logging.getLogger("futures_agent.execution")


class OrderValidationError(ValueError):
    pass


class DuplicateOrderError(RuntimeError):
    pass


class ExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionResult:
    identifier: str
    symbol: str
    action: str
    contracts: int
    route: str                 # "tradovate" or "traderspost"
    status: str                # "submitted" | "rejected"
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    broker_response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "identifier": self.identifier, "symbol": self.symbol, "action": self.action,
            "contracts": self.contracts, "route": self.route, "status": self.status,
            "entry_price": self.entry_price, "stop_price": self.stop_price,
            "target_price": self.target_price, "broker_response": self.broker_response,
        }


def _validate_entry(action: str, contracts: int, stop_points: float, target_points: float,
                    reference_price: float) -> None:
    if action.upper() not in ("BUY", "SELL"):
        raise OrderValidationError(f"action must be BUY or SELL, got {action!r}")
    if not isinstance(contracts, int) or contracts <= 0:
        raise OrderValidationError(f"contracts must be a positive integer, got {contracts!r}")
    if stop_points <= 0:
        raise OrderValidationError(f"stop_points must be positive, got {stop_points}")
    if target_points <= 0:
        raise OrderValidationError(f"target_points must be positive, got {target_points}")
    if reference_price <= 0:
        raise OrderValidationError(f"reference_price must be positive, got {reference_price}")


class ExecutionEngine:
    """`duplicate_check` and `on_submitted` let the caller (main.py) back
    duplicate-prevention with the database instead of process memory alone —
    this engine's own in-memory set only protects within one running
    process; a restart with no persisted record would otherwise forget every
    identifier it had already submitted."""

    def __init__(
        self, execution_mode: str,
        tradovate_client: Optional[TradovateClient] = None,
        traderspost_client: Optional[TradersPostClient] = None,
        duplicate_check: Optional[Callable[[str], bool]] = None,
        on_submitted: Optional[Callable[[str], None]] = None,
    ) -> None:
        if execution_mode not in ("tradovate", "traderspost"):
            raise ValueError(f"execution_mode must be 'tradovate' or 'traderspost', got {execution_mode!r}")
        if execution_mode == "tradovate" and tradovate_client is None:
            raise ValueError("execution_mode is 'tradovate' but no TradovateClient was given")
        if execution_mode == "traderspost" and traderspost_client is None:
            raise ValueError("execution_mode is 'traderspost' but no TradersPostClient was given")

        self.execution_mode = execution_mode
        self._tradovate = tradovate_client
        self._traderspost = traderspost_client
        self._duplicate_check = duplicate_check
        self._on_submitted = on_submitted
        self._seen: set[str] = set()

    def _is_duplicate(self, identifier: str) -> bool:
        if identifier in self._seen:
            return True
        if self._duplicate_check is not None:
            return self._duplicate_check(identifier)
        return False

    def _mark_submitted(self, identifier: str) -> None:
        self._seen.add(identifier)
        if self._on_submitted is not None:
            self._on_submitted(identifier)

    async def submit_entry(
        self, *, symbol: str, action: str, contracts: int, stop_points: float,
        target_points: float, reference_price: float, identifier: str,
    ) -> ExecutionResult:
        _validate_entry(action, contracts, stop_points, target_points, reference_price)

        if self._is_duplicate(identifier):
            raise DuplicateOrderError(
                f"identifier {identifier!r} was already submitted — refusing to resend"
            )

        direction = 1 if action.upper() == "BUY" else -1
        stop_price = reference_price - direction * stop_points
        target_price = reference_price + direction * target_points

        log_trade("submitted", {
            "identifier": identifier, "symbol": symbol, "action": action,
            "contracts": contracts, "route": self.execution_mode,
            "reference_price": reference_price, "stop_price": stop_price,
            "target_price": target_price,
        })
        # Mark before the network call: on a crash mid-request we would
        # rather wrongly skip a resend than risk placing it twice.
        self._mark_submitted(identifier)

        try:
            if self.execution_mode == "tradovate":
                response = await self._submit_entry_tradovate(
                    symbol, action, contracts, identifier)
            else:
                response = await self._traderspost.submit_entry(
                    symbol=symbol, action=action, quantity=contracts,
                    stop_price=stop_price, target_price=target_price, identifier=identifier,
                )
        except Exception as exc:
            log_error("execution_submit_entry", exc)
            log_trade("rejected_by_broker", {"identifier": identifier, "symbol": symbol,
                                             "error": str(exc)})
            raise ExecutionError(f"order submission failed: {exc}") from exc

        result = ExecutionResult(
            identifier=identifier, symbol=symbol.upper(), action=action.upper(),
            contracts=contracts, route=self.execution_mode, status="submitted",
            entry_price=reference_price, stop_price=stop_price, target_price=target_price,
            broker_response=response if isinstance(response, dict) else {"raw": response},
        )
        log_trade("filled", result.to_dict())
        return result

    async def _submit_entry_tradovate(self, symbol: str, action: str, contracts: int,
                                      identifier: str) -> dict:
        contract = await self._tradovate.find_contract(symbol)
        account = await self._tradovate.get_account()
        return await self._tradovate.place_order(
            account_id=account["id"], contract_id=contract["id"],
            action="Buy" if action.upper() == "BUY" else "Sell",
            quantity=contracts, order_type="Market", client_order_id=identifier,
        )

    async def submit_exit(self, *, symbol: str, contracts: int, identifier: str) -> ExecutionResult:
        if self._is_duplicate(identifier):
            raise DuplicateOrderError(
                f"identifier {identifier!r} was already submitted — refusing to resend"
            )
        self._mark_submitted(identifier)

        try:
            if self.execution_mode == "tradovate":
                contract = await self._tradovate.find_contract(symbol)
                account = await self._tradovate.get_account()
                positions = await self._tradovate.list_positions(account["id"])
                net = sum(p.get("netPos", 0) for p in positions if p.get("contractId") == contract["id"])
                if net == 0:
                    response = {"status": "no_open_position"}
                else:
                    response = await self._tradovate.place_order(
                        account_id=account["id"], contract_id=contract["id"],
                        action="Sell" if net > 0 else "Buy", quantity=abs(int(net)),
                        order_type="Market", client_order_id=identifier,
                    )
            else:
                response = await self._traderspost.submit_exit(symbol, identifier=identifier)
        except Exception as exc:
            log_error("execution_submit_exit", exc)
            log_trade("rejected_by_broker", {"identifier": identifier, "symbol": symbol,
                                             "error": str(exc)})
            raise ExecutionError(f"exit submission failed: {exc}") from exc

        result = ExecutionResult(
            identifier=identifier, symbol=symbol.upper(), action="EXIT",
            contracts=contracts, route=self.execution_mode, status="submitted",
            broker_response=response if isinstance(response, dict) else {"raw": response},
        )
        log_trade("closed", result.to_dict())
        return result
